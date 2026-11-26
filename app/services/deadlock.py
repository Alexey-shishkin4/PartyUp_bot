from datetime import datetime

from app.domain.deadlock import DeadlockStats
from app.domain.errors import DomainError
from app.persistence.models import DeadlockProfile, User, utcnow


class DeadlockProfiles:
    def __init__(self, db, lock):
        self.db = db
        self.lock = lock

    def get(self, user_id: int, steam_id: str) -> DeadlockProfile | None:
        with self.db.session() as session:
            profile = session.get(DeadlockProfile, user_id)
            return profile if profile and profile.steam_id == steam_id else None

    def save(
        self, user_id: int, steam_id: str, stats: DeadlockStats, *, expected_checked_at: datetime | None
    ) -> DeadlockProfile:
        with self.lock, self.db.session.begin() as session:
            user = session.get(User, user_id, with_for_update=True)
            if not user or user.banned or user.is_test or user.steam_id != steam_id:
                raise DomainError("Steam уже отключён или изменён. Открой /deadlock ещё раз.")
            profile = session.get(DeadlockProfile, user_id)
            if profile and profile.steam_id != steam_id:
                profile.match_count = profile.match_seconds = None
            elif (profile.checked_at if profile else None) != expected_checked_at:
                raise DomainError("Статистика уже обновилась. Открой /deadlock ещё раз.")
            if profile is None:
                profile = DeadlockProfile(user_id=user_id)
                session.add(profile)
            profile.steam_id = steam_id
            profile.rank_badge = stats.rank_badge
            profile.ranked_at = stats.ranked_at
            if stats.match_count is not None:
                profile.match_count = stats.match_count
                profile.match_seconds = stats.match_seconds
            profile.checked_at = utcnow()
            return profile
