import hashlib
import secrets
import threading
from datetime import timedelta

from sqlalchemy import delete, func, or_, select

from .db import Database
from .models import Block, Event, FSMRecord, Game, LoginTicket, Match, Reaction, Report, User, UserGame, utcnow
from .recommender import AffinityModel, compatible, score
from .schemas import ProfileInput

PURPOSES = {"friends": "Друзья и тиммейты", "romance": "Отношения", "both": "Общение и отношения"}
PHOTO_UNCHANGED = object()


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


class DomainError(RuntimeError):
    pass


class Service:
    def __init__(self, db: Database, model: AffinityModel):
        self.db, self.model = db, model
        # The shipped runtime is one process. Serialize match creation and ticket consumption.
        self.lock = threading.RLock()

    def user_by_telegram(self, telegram_id: int):
        with self.db.session() as session:
            return session.scalar(select(User).where(User.telegram_id == telegram_id))

    def user(self, user_id: int):
        with self.db.session() as session:
            return session.get(User, user_id)

    def save_profile(self, telegram_id: int, profile: ProfileInput, username=None, demo=False,
                     photo_file_id=PHOTO_UNCHANGED, test_profile=False):
        if photo_file_id is not PHOTO_UNCHANGED:
            self.validate_photo(photo_file_id)
        with self.lock, self.db.session.begin() as session:
            user = session.scalar(select(User).where(User.telegram_id == telegram_id))
            if user and user.banned:
                raise DomainError("Анкета заблокирована модератором")
            if user and user.is_test != test_profile:
                raise DomainError("Обычные и тестовые анкеты редактируются отдельно")
            games = list(session.scalars(select(Game).where(Game.app_id.in_(profile.game_ids))))
            if len(games) != len(profile.game_ids):
                raise DomainError("Одна из выбранных игр отсутствует в каталоге")
            values = profile.model_dump(exclude={"game_ids"})
            if user is None:
                user = User(telegram_id=telegram_id, is_demo=demo, is_test=test_profile, **values)
                session.add(user)
                session.flush()
            else:
                for key, value in values.items():
                    setattr(user, key, value)
            user.username = username
            if photo_file_id is not PHOTO_UNCHANGED:
                user.photo_file_id = photo_file_id
            user.last_active = utcnow()
            for entry in user.games:
                entry.selected = entry.app_id in profile.game_ids
            existing = {entry.app_id for entry in user.games}
            for app_id in profile.game_ids:
                if app_id not in existing:
                    user.games.append(UserGame(app_id=app_id, selected=True))
            return user.id

    @staticmethod
    def validate_photo(file_id):
        if file_id is not None and (not isinstance(file_id, str) or not 1 <= len(file_id) <= 1024
                                    or any(char.isspace() for char in file_id)):
            raise DomainError("Некорректная фотография. Отправьте её заново через Telegram")

    def set_photo(self, user_id: int, file_id: str | None):
        self.validate_photo(file_id)
        with self.lock, self.db.session.begin() as session:
            user = session.get(User, user_id)
            if not user or user.banned:
                raise DomainError("Анкета недоступна")
            user.photo_file_id = file_id
            user.last_active = utcnow()

    def photo_file(self, user_id: int):
        with self.db.session() as session:
            return session.scalar(select(User.photo_file_id).where(User.id == user_id))

    def test_photo_file(self, user_id: int):
        from .test_profiles import fixture_photo
        with self.db.session() as session:
            row = session.get(User,user_id)
            return fixture_photo(row.telegram_id) if row and row.is_test and not row.test_photo_override else None

    def cache_test_photo(self,user_id,file_id):
        self.validate_photo(file_id)
        with self.lock,self.db.session.begin() as session:
            user=session.get(User,user_id)
            if user and user.is_test and not user.test_photo_override:
                user.photo_file_id=file_id

    @staticmethod
    def public_profile(user: User):
        test_info = None
        if user.is_test:
            from .test_profiles import fixture_definitions
            test_info = fixture_definitions().get(user.telegram_id)
        selected = [entry for entry in user.games if entry.selected]
        imported = sorted((entry for entry in user.games if entry.imported),
                          key=lambda entry: (-entry.recent_minutes, -entry.minutes, entry.game.name.casefold()))
        visible = {entry.app_id: entry.game.name for entry in [*selected, *imported]}
        status = user.steam_import_status
        if status == "not_loaded" and imported:
            # Existing installations already have Steam games but predate status tracking.
            status = "imported"
        return {
            "id": user.id, "name": user.name, "age": user.age,
            "is_test": user.is_test,
            "photo_source_url": test_info["pin_url"] if test_info and not user.test_photo_override else None,
            "gender": user.gender, "looking_for": user.looking_for,
            "age_min": user.age_min, "age_max": user.age_max,
            "purpose": user.purpose, "purpose_label": PURPOSES[user.purpose],
            "timezone": user.timezone, "start_hour": user.start_hour, "end_hour": user.end_hour,
            "bio": user.bio, "language": user.language, "active": user.active,
            "has_photo": bool(user.photo_file_id) or bool(test_info and not user.test_photo_override),
            "steam_connected": bool(user.steam_id),
            "steam_verified": user.steam_verified,
            "steam_import_status": status,
            "steam_game_count": len(imported),
            "steam_games": [entry.game.name for entry in imported[:10]],
            "selected_games": [entry.game.name for entry in selected],
            "game_ids": [entry.app_id for entry in user.games if entry.selected],
            "games": list(visible.values())[:10],
            "games_count": len(visible),
        }

    @staticmethod
    def blocked(session, left: int, right: int):
        return session.scalar(select(Block).where(or_(
            (Block.actor_id == left) & (Block.target_id == right),
            (Block.actor_id == right) & (Block.target_id == left),
        ))) is not None

    def feed(self, actor_id: int, limit=10, test_only=False, record_impressions=True):
        with self.lock, self.db.session.begin() as session:
            actor = session.get(User, actor_id)
            if not actor or actor.banned or actor.is_test or (not actor.active and not test_only):
                return []
            if record_impressions:
                actor.last_active = utcnow()
            reacted = set(session.scalars(select(Reaction.target_id).where(Reaction.actor_id == actor_id)))
            blocks = session.scalars(select(Block).where(or_(Block.actor_id == actor_id, Block.target_id == actor_id)))
            excluded = reacted | {actor_id}
            for block in blocks:
                excluded.add(block.target_id if block.actor_id == actor_id else block.actor_id)
            query = select(User).where(
                User.active.is_(True), User.banned.is_(False), User.is_demo == actor.is_demo,
                User.id.not_in(excluded), User.last_active >= utcnow() - timedelta(days=30),
            )
            if test_only:
                query = query.where(User.is_test.is_(True))
            else:
                query = query.where(User.age.between(actor.age_min,actor.age_max),User.language == actor.language)
            candidates = session.scalars(query.order_by(User.last_active.desc()).limit(2000))
            ranked = [dict(profile=self.public_profile(candidate), **score(actor, candidate, self.model))
                      for candidate in candidates if test_only or compatible(actor, candidate)]
            ranked.sort(key=lambda row: (-row["score"], row["profile"]["id"]))
            for position, row in enumerate(ranked[:limit]):
                if record_impressions:
                    session.add(Event(actor_id=actor_id, target_id=row["profile"]["id"], kind="test_impression" if row["profile"]["is_test"] else "impression",
                                      details={"position": position, "algorithm": row["algorithm"],
                                               "model": row["model"], "score": row["score"]}))
            return ranked[:limit]

    def reset_test_reactions(self,user_id):
        with self.lock,self.db.session.begin() as session:
            targets = select(User.id).where(User.is_test.is_(True))
            session.execute(delete(Reaction).where(Reaction.actor_id == user_id,Reaction.target_id.in_(targets)))
            session.execute(delete(Event).where(Event.actor_id == user_id,Event.kind.like("test_%")))

    def react(self, actor_id: int, target_id: int, kind: str, reason="Другое"):
        if kind not in {"like", "skip", "block", "report"}:
            raise DomainError("Неизвестное действие")
        with self.lock, self.db.session.begin() as session:
            actor, target = session.get(User, actor_id), session.get(User, target_id)
            if not actor or not target or actor_id == target_id or actor.banned or actor.is_test or target.is_demo != actor.is_demo:
                raise DomainError("Анкета недоступна")
            shown = session.scalar(select(Event.id).where(
                Event.actor_id == actor_id, Event.target_id == target_id,
                Event.kind == ("test_impression" if target.is_test else "impression")
            ).limit(1))
            matched = session.scalar(select(Match.id).where(
                Match.low_id == min(actor_id, target_id), Match.high_id == max(actor_id, target_id)
            ))
            if shown is None and matched is None:
                raise DomainError("Сначала откройте анкету в подборке")
            if target.is_test and kind not in {"like","skip"}:
                raise DomainError("В тестовых анкетах доступны лайк и пропуск")
            if kind in {"like", "skip"} and ((not target.is_test and not compatible(actor, target)) or self.blocked(session, actor_id, target_id)):
                raise DomainError("Анкета больше недоступна")
            count = session.scalar(select(func.count()).select_from(Event).where(
                Event.actor_id == actor_id, Event.kind.in_(["like", "skip", "block", "report", "test_like", "test_skip"]),
                Event.created_at >= utcnow() - timedelta(hours=1),
            ))
            if count >= 120:
                raise DomainError("Лимит действий за час. Вернитесь чуть позже")
            existing = session.get(Reaction, (actor_id, target_id))
            if existing:
                existing.kind = kind
            else:
                session.add(Reaction(actor_id=actor_id, target_id=target_id, kind=kind))
            session.add(Event(actor_id=actor_id, target_id=target_id,
                              kind="test_"+kind if target.is_test else kind, details={}))
            if target.is_test:
                return {"matched":False,"new":False,"test_profile":True}
            if kind in {"block", "report"}:
                if session.get(Block, (actor_id, target_id)) is None:
                    session.add(Block(actor_id=actor_id, target_id=target_id))
                if kind == "report":
                    session.add(Report(actor_id=actor_id, target_id=target_id, reason=reason[:500]))
                return {"matched": False, "new": False}
            reverse = session.get(Reaction, (target_id, actor_id))
            if kind == "like" and reverse and reverse.kind == "like":
                match = session.scalar(select(Match).where(
                    Match.low_id == min(actor_id, target_id), Match.high_id == max(actor_id, target_id)
                ))
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
            rows = session.scalars(select(Match).where(or_(Match.low_id == user_id, Match.high_id == user_id)))
            result = []
            for row in rows:
                peer_id = row.high_id if row.low_id == user_id else row.low_id
                peer = session.get(User, peer_id)
                if peer and not peer.is_test and not peer.banned and not self.blocked(session, user_id, peer_id):
                    result.append({"id": row.id, "profile": self.public_profile(peer)})
            return result

    def match_peer(self, user_id: int, match_id: int):
        for row in self.matches(user_id):
            if row["id"] == match_id:
                return self.user(row["profile"]["id"])
        raise DomainError("Совпадение недоступно или пользователь заблокирован")

    def toggle_active(self, user_id: int):
        with self.db.session.begin() as session:
            user = session.get(User, user_id)
            if not user or user.banned:
                raise DomainError("Анкета недоступна")
            user.active = not user.active
            user.last_active = utcnow()
            return user.active

    def delete_user(self, telegram_id: int):
        with self.lock, self.db.session.begin() as session:
            user = session.scalar(select(User).where(User.telegram_id == telegram_id))
            if user:
                session.delete(user)
            session.execute(delete(FSMRecord).where(FSMRecord.telegram_id == telegram_id))

    def login_ticket(self, user_id: int):
        token = secrets.token_urlsafe(32)
        with self.lock, self.db.session.begin() as session:
            user = session.get(User, user_id)
            if not user or user.banned or user.is_demo:
                raise DomainError("Подключение Steam недоступно")
            session.execute(delete(LoginTicket).where(or_(
                LoginTicket.user_id == user_id, LoginTicket.expires_at < utcnow()
            )))
            session.add(LoginTicket(token_hash=digest(token), user_id=user_id,
                                    expires_at=utcnow() + timedelta(minutes=10)))
        return token

    def begin_login(self, token: str):
        browser = secrets.token_urlsafe(32)
        with self.lock, self.db.session.begin() as session:
            ticket = session.get(LoginTicket, digest(token))
            if not ticket or ticket.expires_at < utcnow() or ticket.browser_hash:
                raise DomainError("Ссылка истекла. Нажмите «Steam» в боте ещё раз")
            ticket.browser_hash = digest(browser)
        return browser

    def verify_ticket(self, token: str, browser: str):
        with self.db.session() as session:
            ticket = session.get(LoginTicket, digest(token))
            if not ticket or ticket.expires_at < utcnow() or not ticket.browser_hash or not secrets.compare_digest(
                ticket.browser_hash, digest(browser)
            ):
                raise DomainError("Сессия Steam истекла или открыта в другом браузере")
            return ticket.user_id

    def link_steam(self, token: str, browser: str, steam_id: str):
        with self.lock, self.db.session.begin() as session:
            user_id = self.verify_ticket(token, browser)
            user = session.get(User, user_id)
            if not user or user.banned:
                raise DomainError("Анкета недоступна")
            duplicate = session.scalar(select(User.id).where(
                User.steam_id == steam_id, User.steam_verified.is_(True), User.id != user_id
            ))
            if duplicate:
                raise DomainError("Этот Steam уже подключён к другой анкете")
            user.steam_id = steam_id
            user.steam_verified = True
            session.execute(delete(LoginTicket).where(LoginTicket.user_id == user_id))
            return user_id

    def link_public_profile(self, user_id: int, steam_id: str):
        """A pasted public URL is an interest source, not proof of ownership.

        Unverified links deliberately do not reserve an account or block its owner.
        """
        with self.lock, self.db.session.begin() as session:
            user = session.get(User, user_id)
            if not user or user.banned or user.is_demo:
                raise DomainError("Добавление ссылки Steam недоступно")
            if user.steam_id != steam_id:
                for entry in list(user.games):
                    if entry.imported:
                        if entry.selected:
                            entry.imported = False
                            entry.minutes = entry.recent_minutes = 0
                        else:
                            user.games.remove(entry)
                user.steam_checked_at = None
                user.steam_import_status = "not_loaded"
                user.steam_verified = False
            user.steam_id = steam_id

    def import_games(self, user_id: int, games: list[dict], expected_steam_id: str | None = None,
                     complete: bool = True):
        with self.lock, self.db.session.begin() as session:
            user = session.get(User, user_id)
            if not user or user.banned:
                raise DomainError("Анкета недоступна")
            if expected_steam_id is not None and user.steam_id != expected_steam_id:
                raise DomainError("Steam уже отключён или изменён. Импорт отменён")
            entries = {entry.app_id: entry for entry in user.games}
            returned = {row["appid"] for row in games}
            for entry in list(user.games):
                if complete and entry.imported and entry.app_id not in returned:
                    if entry.selected:
                        entry.imported = False
                        entry.minutes = entry.recent_minutes = 0
                    else:
                        user.games.remove(entry)
            for row in games:
                app_id = row["appid"]
                if not session.get(Game, app_id):
                    session.add(Game(app_id=app_id, name=row.get("name", f"Steam {app_id}"),
                                     genres=[], aliases=[], coop=False))
                    session.flush()
                entry = entries.get(app_id)
                if entry is None:
                    entry = UserGame(app_id=app_id, selected=False)
                    user.games.append(entry)
                imported_minutes = max(0, int(row.get("playtime_forever", 0)))
                entry.minutes = imported_minutes if complete else max(entry.minutes or 0, imported_minutes)
                entry.recent_minutes = max(0, int(row.get("playtime_2weeks", 0)))
                entry.imported = True
            user.steam_checked_at = utcnow()
            user.steam_import_status = "library" if complete else "recent"
            return len(games)

    def mark_steam_checked(self, user_id: int, status="profile_only"):
        if status not in {"profile_only", "error"}:
            raise DomainError("Неизвестный результат проверки Steam")
        with self.db.session.begin() as session:
            user = session.get(User, user_id)
            if user:
                user.steam_checked_at = utcnow()
                user.steam_import_status = status

    def disconnect_steam(self, user_id: int):
        with self.lock, self.db.session.begin() as session:
            user = session.get(User, user_id)
            if not user:
                raise DomainError("Анкета недоступна")
            user.steam_id = None
            user.steam_verified = False
            user.steam_checked_at = None
            user.steam_import_status = "not_loaded"
            for entry in list(user.games):
                if entry.imported:
                    if entry.selected:
                        entry.imported = False
                        entry.minutes = entry.recent_minutes = 0
                    else:
                        user.games.remove(entry)
            session.execute(delete(LoginTicket).where(LoginTicket.user_id == user_id))

    def record(self, actor_id: int, target_id: int, kind: str, details=None):
        with self.db.session.begin() as session:
            session.add(Event(actor_id=actor_id, target_id=target_id, kind=kind, details=details or {}))
