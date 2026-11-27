from app.domain.errors import DomainError
from app.domain.pubg import profile_rank
from app.domain.steam import PUBG_APP_ID
from app.persistence.game_stats import find_stats, replace_account, save_stats, writable_user
from app.persistence.models import utcnow


class PubgProfiles:
    def __init__(self, db, lock):
        self.db = db
        self.lock = lock

    def get(self, user_id):
        with self.db.session() as session:
            return find_stats(session, user_id, "pubg", PUBG_APP_ID)

    def save(self, user_id, stats, *, expected_checked_at):
        with self.lock, self.db.session.begin() as session:
            user = writable_user(session, user_id)
            current = find_stats(session, user_id, "pubg", PUBG_APP_ID)
            if (current.checked_at if current else None) != expected_checked_at:
                raise DomainError("Профиль PUBG уже изменился. Открой «Добавить игры» ещё раз.")
            return self.store(session, user, stats, checked_at=utcnow(), current=current)

    @staticmethod
    def store(session, user, stats, *, checked_at, current=None):
        identity = stats.account
        account = replace_account(
            session, user, "pubg", identity.account_id,
            replace=bool(current and current.account.details["shard"] != identity.platform),
        )
        account.display_name = identity.nickname
        account.details = {"shard": identity.platform}
        account.checked_at = checked_at
        row = save_stats(session, account, PUBG_APP_ID, current)
        details = dict(row.details)
        if stats.playtime is not None:
            row.playtime_minutes = sum(stats.playtime.mode_seconds.values()) // 60
            details.update(mode_seconds=stats.playtime.mode_seconds, playtime_source=stats.playtime.source,
                           playtime_at=account.checked_at.isoformat())
        if stats.ranking is not None:
            details.update(ranks=stats.ranking.modes, rank_source=stats.ranking.source,
                           season_id=stats.ranking.season_id, rank_at=account.checked_at.isoformat())
        ranks = details.get("ranks", {})
        seconds = details.get("mode_seconds", {})
        row.rank = profile_rank(ranks, seconds)
        row.source = details.get("rank_source") or details.get("playtime_source")
        row.scope = "ranked_season" if details.get("season_id") else "available_history"
        row.checked_at = account.checked_at
        row.details = details
        return row

    def disconnect(self, user_id):
        with self.lock, self.db.session.begin() as session:
            user = writable_user(session, user_id)
            account = user.game_account("pubg")
            if account:
                user.accounts.remove(account)
