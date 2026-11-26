from sqlalchemy import delete, select
from sqlalchemy.orm import object_session

from app.domain.dota import DOTA_APP_ID, dota_game_name
from app.domain.errors import DomainError
from app.domain.matching import CARD_GAME_LIMIT, steam_card_game_ids
from app.domain.profiles import PHOTO_UNCHANGED, PURPOSES, ProfileInput
from app.domain.steam import CS2_APP_ID, DEADLOCK_APP_ID, PUBG_APP_ID
from app.persistence.models import FSMRecord, Game, User, UserGame, utcnow

from .fixture_catalog import fixture_definitions, fixture_photo
from .game_labels import read_game_labels


class ProfileService:
    def __init__(self, db, lock, steam):
        self.db = db
        self.lock = lock
        self.steam = steam

    def catalog(self):
        return self.db.catalog()

    def user_by_telegram(self, telegram_id: int):
        with self.db.session() as session:
            return session.scalar(select(User).where(User.telegram_id == telegram_id))

    def user(self, user_id: int):
        with self.db.session() as session:
            return session.get(User, user_id)

    def save_profile(
        self,
        telegram_id: int,
        profile: ProfileInput | None,
        username=None,
        photo_file_id=PHOTO_UNCHANGED,
        test_profile=False,
        *,
        share_username=None,
        changes=None,
        steam_profile=None,
        use_default_avatar=False,
        schedule_set=None,
        allow_empty_games=False,
        test_library=(),
    ):
        if photo_file_id is not PHOTO_UNCHANGED:
            self.validate_photo(photo_file_id)
        with self.lock, self.db.session.begin() as session:
            user = session.scalar(select(User).where(User.telegram_id == telegram_id))
            if user and user.banned:
                raise DomainError("Анкета заблокирована модератором")
            if user and user.is_test != test_profile:
                raise DomainError("Обычные и тестовые анкеты редактируются отдельно")
            if changes is not None:
                if not user:
                    raise DomainError("Анкета недоступна")
                allow_empty = "game_ids" not in changes or any(
                    entry.effective_minutes or entry.recent_minutes for entry in user.games
                )
                profile = ProfileInput.model_validate(
                    {**self.public_profile(user, linked_games={}), **changes},
                    context={"allow_empty_games": allow_empty},
                )
            if profile is None:
                raise DomainError("Нет данных анкеты")
            values = profile.model_dump(exclude={"game_ids"})
            creating = user is None
            if creating:
                user = User(telegram_id=telegram_id, is_test=test_profile, **values)
                session.add(user)
                session.flush()
            else:
                for key, value in values.items():
                    setattr(user, key, value)
            if steam_profile is not None:
                self.steam.store(session, user, steam_profile)
            if creating and test_profile and test_library:
                self.steam.store_games(session, user, test_library, complete=True)
            if (
                creating
                and not allow_empty_games
                and not profile.game_ids
                and not any(entry.effective_minutes or entry.recent_minutes for entry in user.games)
            ):
                raise DomainError("Выбери хотя бы одну игру или загрузи наигранные часы из Steam")
            games = list(session.scalars(select(Game.app_id).where(Game.app_id.in_(profile.game_ids))))
            if len(games) != len(profile.game_ids):
                raise DomainError("Одна из выбранных игр отсутствует в каталоге")
            user.username = username
            if share_username is not None:
                user.share_username = share_username
            if photo_file_id is not PHOTO_UNCHANGED:
                user.photo_file_id = photo_file_id
                user.use_default_avatar = use_default_avatar and photo_file_id is None
            if schedule_set is not None:
                user.schedule_set = schedule_set
            elif changes and {"start_hour", "end_hour"} & changes.keys():
                user.schedule_set = True
            user.last_active = utcnow()
            for entry in user.games:
                entry.selected = entry.app_id in profile.game_ids
            existing = {entry.app_id for entry in user.games}
            for app_id in profile.game_ids:
                if app_id not in existing:
                    user.games.append(UserGame(app_id=app_id, selected=True))
            return user.id

    def update_telegram_account(self, telegram_id, username, *, share_username=None):
        with self.db.session.begin() as session:
            user = session.scalar(select(User).where(User.telegram_id == telegram_id))
            if user and not user.banned and not user.is_test:
                user.username = username
                if share_username is not None:
                    user.share_username = share_username
            return user

    @staticmethod
    def validate_photo(file_id):
        if file_id is not None and (
            not isinstance(file_id, str)
            or not 1 <= len(file_id) <= 1024
            or any(char.isspace() for char in file_id)
        ):
            raise DomainError("Некорректная фотография. Отправьте её заново через Telegram")

    def set_photo(self, user_id: int, file_id: str | None, *, use_default_avatar=False):
        self.validate_photo(file_id)
        with self.lock, self.db.session.begin() as session:
            user = session.get(User, user_id)
            if not user or user.banned:
                raise DomainError("Анкета недоступна")
            user.photo_file_id = file_id
            user.use_default_avatar = use_default_avatar and file_id is None
            user.last_active = utcnow()

    def photo_file(self, user_id: int):
        with self.db.session() as session:
            return session.scalar(select(User.photo_file_id).where(User.id == user_id))

    def test_photo_file(self, user_id: int):
        with self.db.session() as session:
            row = session.get(User, user_id)
            return (
                fixture_photo(row.telegram_id)
                if row and row.is_test and not row.test_photo_override
                else None
            )

    def cache_test_photo(self, user_id, file_id):
        self.validate_photo(file_id)
        with self.lock, self.db.session.begin() as session:
            user = session.get(User, user_id)
            if user and user.is_test and not user.test_photo_override:
                user.photo_file_id = file_id

    def public_profile(self, user: User, *, linked_games=None):
        if linked_games is None:
            session = object_session(user)
            if session is not None:
                linked_games = read_game_labels(session, [user.id])[user.id]
            else:
                with self.db.session() as session:
                    linked_games = read_game_labels(session, [user.id])[user.id]
        test_info = None
        if user.is_test:
            test_info = fixture_definitions().get(user.telegram_id)
        selected = sorted((entry for entry in user.games if entry.selected), key=lambda entry: entry.app_id)
        imported = [entry for entry in user.games if entry.imported]
        automatic = [entry for entry in user.games if entry.imported or entry.dota_match_minutes]
        highlights = steam_card_game_ids(
            (entry.app_id, entry.effective_minutes, entry.recent_minutes) for entry in automatic
        )
        entries = {entry.app_id: entry for entry in user.games}
        card_games = [entries[app_id] for app_id in highlights] or selected[:CARD_GAME_LIMIT]
        visible = {
            entry.app_id: dota_game_name(entry.app_id, entry.game.name, user.dota_rank_tier) for entry in card_games
        }
        if user.steam_id:
            for app_id in (DOTA_APP_ID, CS2_APP_ID, DEADLOCK_APP_ID, PUBG_APP_ID):
                entry = entries.get(app_id)
                if entry and app_id not in visible and (entry.effective_minutes or entry.recent_minutes):
                    visible[app_id] = dota_game_name(app_id, entry.game.name, user.dota_rank_tier)
        for app_id, (name, rank) in linked_games.items():
            visible[app_id] = f"{name} ({rank})" if rank else name
        return {
            "id": user.id,
            "name": user.name,
            "age": user.age,
            "is_test": user.is_test,
            "photo_source_url": test_info.get("pin_url")
            if test_info and not user.test_photo_override
            else None,
            "gender": user.gender,
            "looking_for": user.looking_for,
            "age_min": user.age_min,
            "age_max": user.age_max,
            "purpose": user.purpose,
            "purpose_label": PURPOSES[user.purpose],
            "timezone": user.timezone,
            "start_hour": user.start_hour,
            "end_hour": user.end_hour,
            "schedule_set": user.schedule_set,
            "bio": user.bio,
            "language": user.language,
            "active": user.active,
            "has_photo": bool(
                user.photo_file_id or user.use_default_avatar or (test_info and not user.test_photo_override)
            ),
            "use_default_avatar": user.use_default_avatar,
            "steam_connected": bool(user.steam_id),
            "steam_import_status": user.steam_import_status,
            "steam_game_count": len(imported),
            "dota_match_estimate": any(item.dota_match_minutes and not item.minutes for item in automatic),
            "selected_games": [entry.game.name for entry in selected],
            "game_ids": [entry.app_id for entry in selected],
            "games": list(visible.values()),
            "games_count": len(visible),
        }

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
