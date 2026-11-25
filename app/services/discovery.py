from datetime import timedelta

from sqlalchemy import delete, func, or_, select

from app.domain.errors import DomainError
from app.domain.matching import compatible, score
from app.persistence.models import Block, Event, Game, Match, Reaction, Report, User, UserGame, utcnow


class DiscoveryService:
    def __init__(self, db, lock, profiles, *, include_test_profiles):
        self.db = db
        self.lock = lock
        self.profiles = profiles
        self.include_test_profiles = include_test_profiles

    @staticmethod
    def current_game(user):
        if user.looking_until and user.looking_until > utcnow():
            return user.looking_for_game
        return None

    def current_game_name(self, app_id):
        with self.db.session() as session:
            return session.scalar(select(Game.name).where(Game.app_id == app_id))

    def set_current_game(self, user_id, app_id):
        with self.db.session.begin() as session:
            user = session.get(User, user_id)
            if not user or user.banned or user.is_test:
                raise DomainError("Анкета недоступна")
            game = session.get(Game, app_id) if app_id is not None else None
            if app_id is not None and game is None:
                raise DomainError("Игра не найдена")
            user.looking_for_game = app_id
            user.looking_until = utcnow() + timedelta(hours=4) if game else None
            return game.name if game else None

    def set_profile_alert(self, user_id, enabled):
        with self.db.session.begin() as session:
            user = session.get(User, user_id)
            if user and not user.banned and not user.is_test:
                user.notify_new_profiles = enabled

    def pending_profile_alerts(self):
        with self.db.session() as session:
            users = session.scalars(
                select(User).where(
                    User.notify_new_profiles.is_(True),
                    User.active.is_(True),
                    User.banned.is_(False),
                    User.is_test.is_(False),
                )
            )
            return [(user.id, user.telegram_id) for user in users]

    @staticmethod
    def blocked(session, left: int, right: int):
        return (
            session.scalar(
                select(Block).where(
                    or_(
                        (Block.actor_id == left) & (Block.target_id == right),
                        (Block.actor_id == right) & (Block.target_id == left),
                    )
                )
            )
            is not None
        )

    def feed(
        self,
        actor_id: int,
        limit=10,
        test_only=False,
        record_impressions=True,
        *,
        real_only=False,
        availability_only=False,
        preferred_id=None,
    ):
        with self.lock, self.db.session.begin() as session:
            actor = session.get(User, actor_id)
            if not actor or actor.banned or actor.is_test or (not actor.active and not test_only):
                return False if availability_only else []
            if record_impressions:
                actor.last_active = utcnow()
            reacted = set(session.scalars(select(Reaction.target_id).where(Reaction.actor_id == actor_id)))
            blocks = session.scalars(
                select(Block).where(or_(Block.actor_id == actor_id, Block.target_id == actor_id))
            )
            excluded = reacted | {actor_id}
            for block in blocks:
                excluded.add(block.target_id if block.actor_id == actor_id else block.actor_id)
            query = select(User).where(
                User.active.is_(True),
                User.banned.is_(False),
                User.id.not_in(excluded),
                User.last_active >= utcnow() - timedelta(days=30),
            )
            focus = None
            if test_only:
                query = query.where(User.is_test.is_(True))
            else:
                query = query.where(
                    User.age.between(actor.age_min, actor.age_max), User.language == actor.language
                )
                focus = self.current_game(actor)
                if focus is not None:
                    plays_game = select(UserGame.user_id).where(
                        UserGame.app_id == focus,
                        or_(UserGame.selected.is_(True), UserGame.recent_minutes > 0),
                    )
                    query = query.where(
                        User.is_test.is_(False),
                        or_(
                            User.id.in_(plays_game),
                            (User.looking_for_game == focus) & (User.looking_until > utcnow()),
                        ),
                    )
            if real_only or not self.include_test_profiles:
                query = query.where(User.is_test.is_(False))
            if preferred_id is not None:
                query = query.where(User.id == preferred_id)
            candidates = session.scalars(query.order_by(User.last_active.desc()).limit(2000))
            if availability_only:
                return any(compatible(actor, candidate) for candidate in candidates)
            ranked = [
                dict(
                    profile=self.profiles.public_profile(candidate),
                    **score(actor, candidate),
                    same_game_now=focus is not None and self.current_game(candidate) == focus,
                )
                for candidate in candidates
                if test_only or compatible(actor, candidate)
            ]
            ranked.sort(
                key=lambda row: (
                    not row["same_game_now"],
                    -row["score"],
                    -row["components"]["time"],
                    row["profile"]["id"],
                )
            )
            for position, row in enumerate(ranked[:limit]):
                if record_impressions:
                    session.add(
                        Event(
                            actor_id=actor_id,
                            target_id=row["profile"]["id"],
                            kind="test_impression" if row["profile"]["is_test"] else "impression",
                            details={
                                "position": position,
                                "algorithm": row["algorithm"],
                                "model": row["model"],
                                "score": row["score"],
                            },
                        )
                    )
            return ranked[:limit]

    def reset_test_reactions(self, user_id):
        with self.lock, self.db.session.begin() as session:
            targets = select(User.id).where(User.is_test.is_(True))
            session.execute(
                delete(Reaction).where(Reaction.actor_id == user_id, Reaction.target_id.in_(targets))
            )
            session.execute(delete(Event).where(Event.actor_id == user_id, Event.kind.like("test_%")))

    def _latest_skip(self, session, actor_id, test_only):
        kinds = (
            ("test_like", "test_skip")
            if test_only
            else ("like", "skip", "block", "report", "test_like", "test_skip")
        )
        latest = session.scalar(
            select(Event)
            .where(
                Event.actor_id == actor_id,
                Event.kind.in_(kinds),
                Event.details["gallery"].as_boolean().is_(True)
                if test_only else Event.details["gallery"].as_boolean().is_not(True),
            )
            .order_by(Event.id.desc())
            .limit(1)
        )
        if latest and latest.kind in {"skip", "test_skip"}:
            reaction = session.get(Reaction, (actor_id, latest.target_id))
            if reaction and reaction.kind == "skip":
                return reaction
        return None

    def can_undo_skip(self, actor_id):
        with self.db.session() as session:
            return self._latest_skip(session, actor_id, False) is not None

    def undo_skip(self, actor_id, *, test_only=False):
        with self.lock, self.db.session.begin() as session:
            reaction = self._latest_skip(session, actor_id, test_only)
            if reaction:
                session.delete(reaction)
                return reaction.target_id
        return None

    def react(self, actor_id: int, target_id: int, kind: str, reason="Другое", *, fresh=False, gallery=False):
        if kind not in {"like", "skip", "block", "report"}:
            raise DomainError("Неизвестное действие")
        with self.lock, self.db.session.begin() as session:
            actor, target = session.get(User, actor_id), session.get(User, target_id)
            if (
                not actor
                or not target
                or actor_id == target_id
                or actor.banned
                or actor.is_test
                or (target.is_test and not self.include_test_profiles)
            ):
                raise DomainError("Анкета недоступна")
            shown = session.scalar(
                select(Event.id)
                .where(
                    Event.actor_id == actor_id,
                    Event.target_id == target_id,
                    Event.kind == ("test_impression" if target.is_test else "impression"),
                )
                .limit(1)
            )
            matched = session.scalar(
                select(Match.id).where(
                    Match.low_id == min(actor_id, target_id), Match.high_id == max(actor_id, target_id)
                )
            )
            if shown is None and matched is None:
                raise DomainError("Сначала откройте анкету в подборке")
            if target.is_test and kind not in {"like", "skip"}:
                raise DomainError("В тестовых анкетах доступны лайк и пропуск")
            if kind in {"like", "skip"} and (
                not target.active
                or target.banned
                or self.blocked(session, actor_id, target_id)
                or (not target.is_test and not compatible(actor, target))
                or (not gallery and not actor.active)
            ):
                raise DomainError("Анкета больше недоступна")
            count = session.scalar(
                select(func.count())
                .select_from(Event)
                .where(
                    Event.actor_id == actor_id,
                    Event.kind.in_(["like", "skip", "block", "report", "test_like", "test_skip"]),
                    Event.created_at >= utcnow() - timedelta(hours=1),
                )
            )
            if count >= 120:
                raise DomainError("Лимит действий за час. Вернитесь чуть позже")
            existing = session.get(Reaction, (actor_id, target_id))
            if fresh and existing and kind in {"like", "skip"}:
                raise DomainError("Реакция уже сохранена. Открой поиск заново")
            if existing:
                existing.kind = kind
            else:
                session.add(Reaction(actor_id=actor_id, target_id=target_id, kind=kind))
            session.add(
                Event(
                    actor_id=actor_id,
                    target_id=target_id,
                    kind="test_" + kind if target.is_test else kind,
                    details={"gallery": gallery},
                )
            )
            if target.is_test:
                return {"matched": False, "new": False, "test_profile": True}
            if kind in {"block", "report"}:
                if session.get(Block, (actor_id, target_id)) is None:
                    session.add(Block(actor_id=actor_id, target_id=target_id))
                if kind == "report":
                    session.add(Report(actor_id=actor_id, target_id=target_id, reason=reason[:500]))
                return {"matched": False, "new": False}
            reverse = session.get(Reaction, (target_id, actor_id))
            if kind == "like" and reverse and reverse.kind == "like":
                match = session.scalar(
                    select(Match).where(
                        Match.low_id == min(actor_id, target_id), Match.high_id == max(actor_id, target_id)
                    )
                )
                created = match is None
                if created:
                    match = Match(low_id=min(actor_id, target_id), high_id=max(actor_id, target_id))
                    session.add(match)
                    session.flush()
                return {"matched": True, "new": created, "match_id": match.id}
            return {"matched": False, "new": False}

    def matches(self, user_id: int):
        with self.db.session() as session:
            actor = session.get(User, user_id)
            if not actor or actor.banned or actor.is_test:
                return []
            rows = session.scalars(
                select(Match).where(or_(Match.low_id == user_id, Match.high_id == user_id))
            )
            result = []
            for row in rows:
                peer_id = row.high_id if row.low_id == user_id else row.low_id
                peer = session.get(User, peer_id)
                if (
                    peer
                    and not peer.is_test
                    and not peer.banned
                    and not self.blocked(session, user_id, peer_id)
                ):
                    result.append({"id": row.id, "profile": self.profiles.public_profile(peer)})
            return result

    def match_peer(self, user_id: int, match_id: int):
        with self.db.session() as session:
            match = session.get(Match, match_id)
            if match and user_id in (match.low_id, match.high_id):
                actor = session.get(User, user_id)
                peer_id = match.high_id if match.low_id == user_id else match.low_id
                peer = session.get(User, peer_id)
                if (
                    actor
                    and peer
                    and not actor.banned
                    and not peer.banned
                    and not actor.is_test
                    and not peer.is_test
                    and not self.blocked(session, user_id, peer_id)
                ):
                    return peer
        raise DomainError("Совпадение недоступно или пользователь заблокирован")

    def record(self, actor_id: int, target_id: int, kind: str, details=None):
        with self.db.session.begin() as session:
            session.add(Event(actor_id=actor_id, target_id=target_id, kind=kind, details=details or {}))
