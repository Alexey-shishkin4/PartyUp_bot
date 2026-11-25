from sqlalchemy import delete

from app.domain.errors import DomainError
from app.domain.fortnite import FortniteSnapshot
from app.persistence.models import FortniteAccount, FortniteModeStats, User


class FortniteProfiles:
    def __init__(self, db, lock):
        self.db = db
        self.lock = lock

    def get(self, user_id: int) -> FortniteAccount | None:
        with self.db.session() as session:
            return session.get(FortniteAccount, user_id)

    def save(self, user_id: int, snapshot: FortniteSnapshot, *, expected_account_id=None):
        with self.lock, self.db.session.begin() as session:
            user = session.get(User, user_id)
            if not user or user.banned or user.is_test:
                raise DomainError("Анкета недоступна")
            profile = session.get(FortniteAccount, user_id)
            if expected_account_id is not None and (not profile or profile.account_id != expected_account_id):
                raise DomainError("Профиль Fortnite уже отключён или изменён")
            if profile and profile.checked_at > snapshot.checked_at:
                return profile
            if profile and profile.account_id != snapshot.account.account_id:
                session.delete(profile)
                session.flush()
                profile = None
            if profile is None:
                profile = FortniteAccount(user_id=user_id, account_id=snapshot.account.account_id, modes=[])
                session.add(profile)
            profile.display_name = snapshot.account.display_name
            profile.checked_at = snapshot.checked_at
            modes = {row.mode: row for row in profile.modes}
            lifetime = snapshot.playtime.modes if snapshot.playtime is not None else {}
            minutes = snapshot.activity.minutes if snapshot.activity is not None else {}
            matches = snapshot.activity.matches if snapshot.activity is not None else {}
            ranks = snapshot.ranks or {}
            for mode in sorted(lifetime.keys() | minutes.keys() | matches.keys() | ranks.keys()):
                if mode not in modes:
                    modes[mode] = FortniteModeStats(mode=mode)
                    profile.modes.append(modes[mode])
            if snapshot.playtime is not None and snapshot.playtime.total_minutes is not None:
                profile.total_minutes = snapshot.playtime.total_minutes
                profile.playtime_source = snapshot.playtime.source
            if snapshot.activity is not None:
                profile.favorite_mode = snapshot.activity.favorite_mode()
                profile.recent_checked_at = snapshot.checked_at
            for mode, row in modes.items():
                if mode in lifetime:
                    row.lifetime_minutes = lifetime[mode]
                if snapshot.activity is not None:
                    row.recent_minutes = minutes.get(mode)
                    row.recent_matches = matches.get(mode)
                if snapshot.ranks is not None:
                    rank = ranks.get(mode)
                    row.rank_name = rank.name if rank else None
                    row.rank_track_id = rank.track_id if rank else None
                    row.rank_source = rank.source if rank else None
                    row.rank_checked_at = snapshot.checked_at if rank else None
            return profile

    def disconnect(self, user_id: int):
        with self.lock, self.db.session.begin() as session:
            session.execute(delete(FortniteAccount).where(FortniteAccount.user_id == user_id))
