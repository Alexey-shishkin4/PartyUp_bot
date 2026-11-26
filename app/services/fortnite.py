from app.domain.errors import DomainError
from app.domain.fortnite import favorite_rank_mode
from app.domain.games import FORTNITE_GAME_ID
from app.persistence.game_stats import find_stats, replace_account, save_stats, writable_user


class FortniteProfiles:
    def __init__(self, db, lock):
        self.db = db
        self.lock = lock

    def get(self, user_id):
        with self.db.session() as session:
            return find_stats(session, user_id, "epic", FORTNITE_GAME_ID)

    def save(self, user_id, snapshot, *, expected_account_id=None):
        with self.lock, self.db.session.begin() as session:
            user = writable_user(session, user_id)
            current = find_stats(session, user_id, "epic", FORTNITE_GAME_ID)
            if expected_account_id is not None and (
                not current or current.account.external_account_id != expected_account_id
            ):
                raise DomainError("Профиль Fortnite уже отключён или изменён")
            if current and current.checked_at > snapshot.checked_at:
                return current
            return self.store(session, user, snapshot, current=current)

    @staticmethod
    def store(session, user, snapshot, *, current=None):
        account = replace_account(session, user, "epic", snapshot.account.account_id)
        account.display_name = snapshot.account.display_name
        account.checked_at = snapshot.checked_at
        row = save_stats(session, account, FORTNITE_GAME_ID, current)
        details = dict(row.details)
        checked_at = snapshot.checked_at.isoformat()
        if snapshot.playtime is not None:
            if snapshot.playtime.total_minutes is not None:
                row.playtime_minutes = snapshot.playtime.total_minutes
                details["playtime_source"] = snapshot.playtime.source
            details["lifetime_modes"] = details.get("lifetime_modes", {}) | snapshot.playtime.modes
        if snapshot.activity is not None:
            details.update(recent_minutes=snapshot.activity.minutes, recent_matches=snapshot.activity.matches,
                           favorite_mode=snapshot.activity.favorite_mode(), recent_checked_at=checked_at)
        if snapshot.ranks is not None:
            details["ranks"] = {
                mode: {"rank": rank.name, "track_id": rank.track_id, "source": rank.source,
                       "checked_at": checked_at}
                for mode, rank in snapshot.ranks.items()
            }
        favorite = details.get("favorite_mode")
        ranks = details.get("ranks", {})
        mode = favorite_rank_mode(favorite, {mode: data["rank"] for mode, data in ranks.items()})
        rank = ranks.get(mode, {})
        row.rank = rank.get("rank")
        row.source = rank.get("source") or details.get("playtime_source")
        row.scope = "favorite_mode" if favorite else "available_history"
        row.checked_at = snapshot.checked_at
        row.details = details
        return row

    def disconnect(self, user_id):
        with self.lock, self.db.session.begin() as session:
            user = writable_user(session, user_id)
            account = user.game_account("epic")
            if account:
                user.accounts.remove(account)
