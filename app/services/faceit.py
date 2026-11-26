from datetime import datetime

from app.domain.errors import DomainError
from app.domain.faceit import FaceitStats
from app.persistence.models import FaceitProfile, User, utcnow


class FaceitProfiles:
    def __init__(self, db, lock):
        self.db = db
        self.lock = lock

    def get(self, user_id: int) -> FaceitProfile | None:
        with self.db.session() as session:
            return session.get(FaceitProfile, user_id)

    def save(
        self, user_id: int, stats: FaceitStats, *, expected_checked_at: datetime | None, expected_steam_id=None,
    ) -> FaceitProfile:
        with self.lock, self.db.session.begin() as session:
            user = session.get(User, user_id, with_for_update=True)
            if not user or user.banned or user.is_test:
                raise DomainError("Анкета недоступна.")
            if expected_steam_id is not None and user.steam_id != expected_steam_id:
                raise DomainError("Привязка Steam изменилась. Открой «Добавить игры» ещё раз.")
            profile = session.get(FaceitProfile, user_id)
            if (profile.checked_at if profile else None) != expected_checked_at:
                raise DomainError("Профиль FACEIT уже изменился. Открой «Добавить игры» ещё раз.")
            if profile is None:
                profile = FaceitProfile(user_id=user_id)
                session.add(profile)
            if stats.player_id != profile.player_id or stats.steam_id is not None:
                profile.steam_id = stats.steam_id
            profile.player_id = stats.player_id
            profile.nickname = stats.nickname
            profile.skill_level = stats.skill_level
            profile.elo = stats.elo
            profile.match_count = stats.match_count
            profile.wins = stats.wins
            profile.win_rate = stats.win_rate
            profile.source = stats.source
            profile.checked_at = utcnow()
            return profile

    def disconnect(self, user_id: int) -> None:
        with self.lock, self.db.session.begin() as session:
            if session.get(User, user_id, with_for_update=True):
                profile = session.get(FaceitProfile, user_id)
                if profile:
                    session.delete(profile)
