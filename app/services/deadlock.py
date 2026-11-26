from app.domain.deadlock import rank_name
from app.domain.errors import DomainError
from app.domain.steam import DEADLOCK_APP_ID
from app.persistence.game_stats import find_stats, save_stats, writable_user
from app.persistence.models import utcnow


class DeadlockProfiles:
    def __init__(self, db, lock):
        self.db = db
        self.lock = lock

    def get(self, user_id, steam_id):
        with self.db.session() as session:
            row = find_stats(session, user_id, "steam", DEADLOCK_APP_ID)
            return row if row and row.account.external_account_id == steam_id else None

    def save(self, user_id, steam_id, stats, *, expected_checked_at):
        with self.lock, self.db.session.begin() as session:
            user = writable_user(session, user_id)
            account = user.game_account("steam")
            if not account or account.external_account_id != steam_id:
                raise DomainError("Steam уже отключён или изменён. Открой «Добавить игры» ещё раз.")
            current = find_stats(session, user_id, "steam", DEADLOCK_APP_ID)
            if (current.checked_at if current else None) != expected_checked_at:
                raise DomainError("Статистика уже обновилась. Открой «Добавить игры» ещё раз.")
            return self.store(session, account, stats, checked_at=utcnow(), current=current)

    @staticmethod
    def store(session, account, stats, *, checked_at, current=None):
        row = save_stats(session, account, DEADLOCK_APP_ID, current)
        row.rank = rank_name(stats.rank_badge)
        row.source = "deadlock"
        row.scope = "available_history"
        row.checked_at = checked_at
        details = row.details | {
            "rank_badge": stats.rank_badge,
            "ranked_at": stats.ranked_at.isoformat() if stats.ranked_at else None,
        }
        if stats.match_count is not None:
            row.matches_count = stats.match_count
            row.playtime_minutes = stats.match_seconds // 60 if stats.match_seconds is not None else None
            details["match_seconds"] = stats.match_seconds
        row.details = details
        return row

    def disconnect(self, user_id):
        with self.lock, self.db.session.begin() as session:
            writable_user(session, user_id)
            row = find_stats(session, user_id, "steam", DEADLOCK_APP_ID)
            if row:
                session.delete(row)
