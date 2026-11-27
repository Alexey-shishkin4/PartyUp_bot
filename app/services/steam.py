from sqlalchemy import select

from app.domain.dota import DOTA_APP_ID, dota_rank_name
from app.domain.errors import DomainError
from app.persistence.game_stats import find_stats, replace_account, save_stats
from app.persistence.models import Game, User, UserGame, utcnow


class SteamLibrary:
    def __init__(self, db, lock):
        self.db = db
        self.lock = lock

    @staticmethod
    def _link_public_steam(session, user, steam_id):
        if not user or user.banned:
            raise DomainError("Добавление ссылки Steam недоступно")
        if user.steam_id != steam_id:
            SteamLibrary._clear_steam_data(user)
        return replace_account(session, user, "steam", steam_id)

    @staticmethod
    def _clear_steam_data(user):
        faceit = user.game_account("faceit")
        if faceit and user.steam_id and faceit.details.get("steam_id") == user.steam_id:
            user.accounts.remove(faceit)
        for entry in list(user.games):
            if entry.imported or entry.dota_match_minutes:
                if entry.selected:
                    entry.imported = False
                    entry.minutes = entry.recent_minutes = entry.dota_match_minutes = 0
                else:
                    user.games.remove(entry)

    def save_public_steam(self, user_id, profile, *, expected_steam_id=None):
        with self.lock, self.db.session.begin() as session:
            user = session.get(User, user_id, with_for_update=True)
            if expected_steam_id is not None and (
                not user or user.steam_id != expected_steam_id or profile.steam_id != expected_steam_id
            ):
                raise DomainError("Steam уже отключён или изменён. Импорт отменён")
            self.store(session, user, profile)

    def store(self, session, user, profile):
        account = self._link_public_steam(session, user, profile.steam_id)
        self._store_dota_stats(session, user, account, profile)
        if profile.coverage == "profile_only":
            account.checked_at = utcnow()
            account.status = "profile_only"
        else:
            self.store_games(session, user, profile.games, complete=profile.coverage == "library")

    @staticmethod
    def _store_dota_stats(session, user, account, profile):
        minutes = profile.dota_match_minutes
        if profile.dota_rank_checked or minutes is not None:
            current = find_stats(session, user.id, "steam", DOTA_APP_ID)
            rank = dota_rank_name(profile.dota_rank_tier)
            steam_has_playtime = any(
                game["appid"] == DOTA_APP_ID and (game.get("playtime_forever") or game.get("playtime_2weeks"))
                for game in profile.games
            )
            if current is not None or rank or minutes or steam_has_playtime:
                row = save_stats(session, account, DOTA_APP_ID, current)
                row.source = "opendota"
                row.scope = "available_history"
                row.checked_at = utcnow()
                if profile.dota_rank_checked:
                    row.rank = rank
                    row.details = {"rank_tier": profile.dota_rank_tier}
                if minutes is not None:
                    row.playtime_minutes = minutes
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
                        slug=f"steam-{app_id}",
                        name=row.get("name", f"Steam {app_id}"),
                        genres=[],
                        aliases=[],
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
        account = user.game_account("steam")
        if account:
            account.checked_at = utcnow()
            account.status = "library" if complete else "recent"

    def disconnect_steam(self, user_id: int):
        with self.lock, self.db.session.begin() as session:
            user = session.get(User, user_id, with_for_update=True)
            if not user:
                raise DomainError("Анкета недоступна")
            self._clear_steam_data(user)
            account = user.game_account("steam")
            if account:
                user.accounts.remove(account)
