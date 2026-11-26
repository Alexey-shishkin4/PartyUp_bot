from datetime import datetime

from app.domain.errors import DomainError
from app.domain.pubg import PubgStats
from app.persistence.models import PubgProfile, User, utcnow


class PubgProfiles:
    def __init__(self, db, lock):
        self.db = db
        self.lock = lock

    def get(self, user_id: int) -> PubgProfile | None:
        with self.db.session() as session:
            return session.get(PubgProfile, user_id)

    def save(self, user_id: int, stats: PubgStats, *, expected_checked_at: datetime | None) -> PubgProfile:
        with self.lock, self.db.session.begin() as session:
            user = session.get(User, user_id, with_for_update=True)
            if not user or user.banned or user.is_test:
                raise DomainError("Анкета недоступна.")
            profile = session.get(PubgProfile, user_id)
            if (profile.checked_at if profile else None) != expected_checked_at:
                raise DomainError("Профиль PUBG уже изменился. Открой «Добавить игры» ещё раз.")
            if profile is None:
                profile = PubgProfile(user_id=user_id)
                session.add(profile)
            elif (profile.account_id, profile.platform) != (stats.account.account_id, stats.account.platform):
                profile.mode_seconds = profile.ranks = None
                profile.playtime_source = profile.rank_source = profile.season_id = None
                profile.playtime_at = profile.rank_at = None
            profile.account_id = stats.account.account_id
            profile.nickname = stats.account.nickname
            profile.platform = stats.account.platform
            now = utcnow()
            if stats.playtime is not None:
                profile.mode_seconds = stats.playtime.mode_seconds
                profile.playtime_source = stats.playtime.source
                profile.playtime_at = now
            if stats.ranking is not None:
                profile.ranks = stats.ranking.modes
                profile.rank_source = stats.ranking.source
                profile.season_id = stats.ranking.season_id
                profile.rank_at = now
            profile.checked_at = now
            return profile

    def disconnect(self, user_id: int) -> None:
        with self.lock, self.db.session.begin() as session:
            if session.get(User, user_id, with_for_update=True):
                profile = session.get(PubgProfile, user_id)
                if profile:
                    session.delete(profile)
