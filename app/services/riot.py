from datetime import datetime

from app.domain.errors import DomainError
from app.domain.riot import RiotGame, RiotStats
from app.persistence.models import RiotProfile, User, utcnow


class RiotProfiles:
    def __init__(self, db, lock):
        self.db = db
        self.lock = lock

    def get(self, user_id: int, game: RiotGame) -> RiotProfile | None:
        with self.db.session() as session:
            return session.get(RiotProfile, (user_id, game))

    def save(
        self,
        user_id: int,
        game: RiotGame,
        stats: RiotStats,
        *,
        expected_checked_at: datetime | None,
    ) -> RiotProfile:
        with self.lock, self.db.session.begin() as session:
            user = session.get(User, user_id, with_for_update=True)
            if not user or user.banned or user.is_test:
                raise DomainError("Анкета недоступна.")
            profile = session.get(RiotProfile, (user_id, game))
            if (profile.checked_at if profile else None) != expected_checked_at:
                raise DomainError("Профиль уже изменился. Открой команду игры ещё раз.")
            if profile is None:
                profile = RiotProfile(user_id=user_id, game=game)
                session.add(profile)
            profile.region = stats.region
            profile.game_name = stats.riot_id.name
            profile.tag_line = stats.riot_id.tag
            profile.rank = stats.rank
            profile.match_count = stats.matches
            profile.match_scope = stats.scope
            profile.source = stats.source
            profile.checked_at = utcnow()
            return profile

    def disconnect(self, user_id: int, game: RiotGame) -> None:
        with self.lock, self.db.session.begin() as session:
            if session.get(User, user_id, with_for_update=True):
                profile = session.get(RiotProfile, (user_id, game))
                if profile:
                    session.delete(profile)
