from sqlalchemy import delete, select

from app.domain.dota import DOTA_APP_ID
from app.domain.errors import DomainError
from app.persistence.models import DeadlockProfile, FaceitProfile, Game, User, UserGame, utcnow


class SteamLibrary:
    def __init__(self, db, lock):
        self.db = db
        self.lock = lock

    @staticmethod
    def _link_public_steam(session, user, steam_id):
        if not user or user.banned:
            raise DomainError("Добавление ссылки Steam недоступно")
        if user.steam_id != steam_id:
            SteamLibrary._clear_steam_data(session, user)
        user.steam_id = steam_id

    @staticmethod
    def _clear_steam_data(session, user):
        if user.id is not None:
            session.execute(delete(DeadlockProfile).where(DeadlockProfile.user_id == user.id))
            if user.steam_id:
                session.execute(delete(FaceitProfile).where(
                    FaceitProfile.user_id == user.id, FaceitProfile.steam_id == user.steam_id,
                ))
        for entry in list(user.games):
            if entry.imported or entry.dota_match_minutes:
                if entry.selected:
                    entry.imported = False
                    entry.minutes = entry.recent_minutes = entry.dota_match_minutes = 0
                else:
                    user.games.remove(entry)
        user.steam_checked_at = None
        user.steam_import_status = "not_loaded"
        user.dota_rank_tier = None

    def save_public_steam(self, user_id, profile, *, expected_steam_id=None):
        with self.lock, self.db.session.begin() as session:
            user = session.get(User, user_id)
            if expected_steam_id is not None and (
                not user or user.steam_id != expected_steam_id or profile.steam_id != expected_steam_id
            ):
                raise DomainError("Steam уже отключён или изменён. Импорт отменён")
            self.store(session, user, profile)

    def store(self, session, user, profile):
        self._link_public_steam(session, user, profile.steam_id)
        self._store_dota_stats(user, profile)
        if profile.coverage == "profile_only":
            user.steam_checked_at = utcnow()
            user.steam_import_status = "profile_only"
        else:
            self.store_games(session, user, profile.games, complete=profile.coverage == "library")

    @staticmethod
    def _store_dota_stats(user, profile):
        if profile.dota_rank_checked:
            user.dota_rank_tier = profile.dota_rank_tier
        minutes = profile.dota_match_minutes
        if minutes is None:
            return
        entry = next((game for game in user.games if game.app_id == DOTA_APP_ID), None)
        if entry is None:
            if minutes:
                user.games.append(UserGame(app_id=DOTA_APP_ID, dota_match_minutes=minutes))
            return
        entry.dota_match_minutes = minutes
        if not minutes and not entry.imported and not entry.selected:
            user.games.remove(entry)

    def import_games(
        self, user_id: int, games: list[dict], expected_steam_id: str | None = None, complete: bool = True
    ):
        with self.lock, self.db.session.begin() as session:
            user = session.get(User, user_id)
            if not user or user.banned:
                raise DomainError("Анкета недоступна")
            if expected_steam_id is not None and user.steam_id != expected_steam_id:
                raise DomainError("Steam уже отключён или изменён. Импорт отменён")
            self.store_games(session, user, games, complete=complete)
            return len(games)

    @staticmethod
    def store_games(session, user, games, *, complete):
        entries = {entry.app_id: entry for entry in user.games}
        returned = {row["appid"] for row in games}
        if complete:
            for app_id, entry in entries.items():
                if entry.imported and app_id not in returned:
                    if entry.selected or entry.dota_match_minutes:
                        entry.imported = False
                        entry.minutes = entry.recent_minutes = 0
                    else:
                        user.games.remove(entry)
        existing_games = set(session.scalars(select(Game.app_id).where(Game.app_id.in_(returned))))
        for row in games:
            app_id = row["appid"]
            if app_id not in existing_games:
                session.add(
                    Game(
                        app_id=app_id,
                        name=row.get("name", f"Steam {app_id}"),
                        genres=[],
                        aliases=[],
                        coop=False,
                    )
                )
                existing_games.add(app_id)
            entry = entries.get(app_id)
            if entry is None:
                entry = entries[app_id] = UserGame(app_id=app_id, selected=False)
                user.games.append(entry)
            imported_minutes = max(0, int(row.get("playtime_forever", 0)))
            entry.minutes = imported_minutes if complete else max(entry.minutes or 0, imported_minutes)
            entry.recent_minutes = max(0, int(row.get("playtime_2weeks", 0)))
            entry.imported = True
        user.steam_checked_at = utcnow()
        user.steam_import_status = "library" if complete else "recent"

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
            self._clear_steam_data(session, user)
            user.steam_id = None
