"""Authorized profile operations and diagnostics. Every entry checks the Telegram ID."""

from pydantic import ValidationError
from sqlalchemy import delete, func, select

from app.domain.errors import DomainError
from app.domain.matching import VERSION, compatible, score
from app.domain.profiles import ProfileInput
from app.persistence.models import DeveloperAudit, Event, Game, Match, Reaction, Report, User, UserGame


class DeveloperAccessDenied(DomainError):
    pass


class DeveloperService:
    def __init__(self, services, settings):
        self.services, self.settings = services, settings
        self.db = services.db

    def require(self, actor):
        if actor not in self.settings.developers:
            raise DeveloperAccessDenied("Режим разработчика доступен только разрешённым Telegram ID")

    @staticmethod
    def audit(session, actor, target, action, details=None):
        session.add(
            DeveloperAudit(
                actor_telegram_id=actor, target_user_id=target, action=action, details=details or {}
            )
        )

    def stats(self, actor):
        self.require(actor)
        with self.db.session() as session:

            def total(model, *conditions):
                return session.scalar(select(func.count()).select_from(model).where(*conditions))

            return {
                "profiles": total(User),
                "real": total(User, User.is_test.is_(False)),
                "test": total(User, User.is_test.is_(True)),
                "active": total(User, User.active.is_(True), User.banned.is_(False)),
                "paused": total(User, User.active.is_(False)),
                "banned": total(User, User.banned.is_(True)),
                "matches": total(Match),
                "reactions": total(Reaction),
                "reports": total(Report, Report.resolved.is_(False)),
                "games": total(Game),
                "model": VERSION,
                "database": self.db.engine.dialect.name,
                "steam_api_configured": bool(self.settings.steam_api_key),
            }

    def list_profiles(self, actor, offset=0, scope="all", limit=5):
        self.require(actor)
        if scope not in {"all", "real", "test", "paused", "banned"}:
            raise DomainError("Неизвестный фильтр списка анкет")
        offset = max(0, int(offset))
        limit = max(1, min(10, int(limit)))
        conditions = []
        if scope == "real":
            conditions.append(User.is_test.is_(False))
        if scope == "test":
            conditions.append(User.is_test.is_(True))
        if scope == "paused":
            conditions.append(User.active.is_(False))
        if scope == "banned":
            conditions.append(User.banned.is_(True))
        with self.db.session() as session:
            count = session.scalar(select(func.count()).select_from(User).where(*conditions))
            rows = list(
                session.scalars(select(User).where(*conditions).order_by(User.id).offset(offset).limit(limit))
            )
            return {
                "total": count,
                "offset": offset,
                "scope": scope,
                "profiles": [
                    self.services.profiles.public_profile(row) | {"banned": row.banned} for row in rows
                ],
            }

    def read_profile(self, actor, target):
        self.require(actor)
        with self.db.session() as session:
            user = session.get(User, target)
            if not user:
                raise DomainError("Анкета не найдена")
            return self.services.profiles.public_profile(user) | {
                "telegram_id": user.telegram_id,
                "username": user.username,
                "banned": user.banned,
                "steam_id": user.steam_id,
                "steam_checked_at": user.steam_checked_at,
                "last_active": user.last_active,
            }

    def update_profile(self, actor, target, changes):
        self.require(actor)
        allowed = set(ProfileInput.model_fields) - {"game_ids"}
        if not changes or set(changes) - allowed - {"active", "banned"}:
            raise DomainError("Это поле нельзя менять через редактор анкет")
        for field in {"active", "banned"} & set(changes):
            if type(changes[field]) is not bool:
                raise DomainError("Статус должен быть true или false")
        with self.services.lock, self.db.session.begin() as session:
            user = session.get(User, target)
            if not user:
                raise DomainError("Анкета не найдена")
            values = self.services.profiles.public_profile(user)
            values.update({key: value for key, value in changes.items() if key in allowed})
            try:
                validated = ProfileInput.model_validate(values, context={"allow_empty_games": True})
            except ValidationError:
                raise DomainError(
                    "Проверь значение: возраст 18–99, корректные интервалы, часовой пояс и длину текста"
                ) from None
            for key, value in changes.items():
                setattr(user, key, getattr(validated, key) if key in allowed else value)
            if {"start_hour", "end_hour"} & changes.keys():
                user.schedule_set = True
            self.audit(session, actor, target, "profile_update", {"fields": sorted(changes)})
        return self.read_profile(actor, target)

    def set_games(self, actor, target, game_ids):
        self.require(actor)
        game_ids = list(dict.fromkeys(game_ids))
        if not 1 <= len(game_ids) <= 10:
            raise DomainError("Выбери от 1 до 10 игр")
        with self.services.lock, self.db.session.begin() as session:
            user = session.get(User, target)
            if not user:
                raise DomainError("Анкета не найдена")
            found = set(session.scalars(select(Game.app_id).where(Game.app_id.in_(game_ids))))
            if found != set(game_ids):
                raise DomainError("Игра отсутствует в каталоге")
            for entry in user.games:
                entry.selected = entry.app_id in found
            existing = {entry.app_id for entry in user.games}
            for app_id in game_ids:
                if app_id not in existing:
                    user.games.append(UserGame(app_id=app_id, selected=True))
            self.audit(session, actor, target, "games_update", {"selected_count": len(game_ids)})

    def set_photo(self, actor, target, file_id):
        self.require(actor)
        self.services.profiles.validate_photo(file_id)
        with self.services.lock, self.db.session.begin() as session:
            user = session.get(User, target)
            if not user:
                raise DomainError("Анкета не найдена")
            user.photo_file_id = file_id
            user.use_default_avatar = False
            if user.is_test:
                user.test_photo_override = True
            self.audit(session, actor, target, "photo_update", {"present": file_id is not None})

    def preview(self, actor, target):
        self.require(actor)
        self.read_profile(actor, target)
        return self.services.discovery.feed(target, limit=5, record_impressions=False)

    def compare(self, actor, left, right):
        self.require(actor)
        a, b = self.services.profiles.user(left), self.services.profiles.user(right)
        if not a or not b:
            raise DomainError("Одна из анкет не найдена")
        return {
            "left": a.id,
            "right": b.id,
            "compatible": compatible(a, b),
            **score(a, b),
        }

    def reset_tests(self, actor):
        self.require(actor)
        user = self.services.profiles.user_by_telegram(actor)
        if not user:
            raise DomainError("Сначала создай свою анкету через /start")
        with self.services.lock, self.db.session.begin() as session:
            # Only the developer's own test reactions; actual matches are untouched.
            target_ids = select(User.id).where(User.is_test.is_(True))
            session.execute(
                delete(Reaction).where(Reaction.actor_id == user.id, Reaction.target_id.in_(target_ids))
            )
            session.execute(delete(Event).where(Event.actor_id == user.id, Event.kind.like("test_%")))
            self.audit(session, actor, user.id, "test_reset")

    def history(self, actor, limit=12):
        self.require(actor)
        with self.db.session() as session:
            return list(
                session.scalars(select(DeveloperAudit).order_by(DeveloperAudit.id.desc()).limit(limit))
            )
