from app.domain.errors import DomainError
from app.domain.steam import CS2_APP_ID
from app.persistence.game_stats import find_stats, replace_account, save_stats, writable_user
from app.persistence.models import utcnow


class FaceitProfiles:
    def __init__(self, db, lock):
        self.db = db
        self.lock = lock

    def get(self, user_id):
        with self.db.session() as session:
            return find_stats(session, user_id, "faceit", CS2_APP_ID)

    def save(self, user_id, stats, *, expected_checked_at, expected_steam_id=None):
        with self.lock, self.db.session.begin() as session:
            user = writable_user(session, user_id)
            if expected_steam_id is not None and user.steam_id != expected_steam_id:
                raise DomainError("Привязка Steam изменилась. Открой «Добавить игры» ещё раз.")
            current = find_stats(session, user_id, "faceit", CS2_APP_ID)
            if (current.checked_at if current else None) != expected_checked_at:
                raise DomainError("Профиль FACEIT уже изменился. Открой «Добавить игры» ещё раз.")
            return self.store(session, user, stats, checked_at=utcnow(), current=current)

    @staticmethod
    def store(session, user, stats, *, checked_at, current=None):
        account = replace_account(session, user, "faceit", stats.player_id)
        account.display_name = stats.nickname
        if stats.steam_id is not None:
            account.details = {"steam_id": stats.steam_id}
        account.checked_at = checked_at
        row = save_stats(session, account, CS2_APP_ID, current)
        row.rank = f"FACEIT {stats.skill_level}" if stats.skill_level is not None else None
        row.matches_count = stats.match_count
        row.source = stats.source
        row.scope = "lifetime"
        row.checked_at = account.checked_at
        row.details = {
            "elo": stats.elo, "skill_level": stats.skill_level, "wins": stats.wins, "win_rate": stats.win_rate,
        }
        return row

    def disconnect(self, user_id):
        with self.lock, self.db.session.begin() as session:
            user = writable_user(session, user_id)
            account = user.game_account("faceit")
            if account:
                user.accounts.remove(account)
