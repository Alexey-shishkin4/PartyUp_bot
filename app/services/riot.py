from datetime import datetime

from app.domain.errors import DomainError
from app.domain.riot import RiotGame, RiotId, RiotStats
from app.persistence.models import RiotProfile, User, utcnow


class RiotProfiles:
    def __init__(self, db, lock):
        self.db = db
        self.lock = lock

    def get(self, user_id: int, game: RiotGame) -> RiotProfile | None:
        with self.db.session() as session:
            return session.get(RiotProfile, (user_id, game))

    def save_account(
        self,
        user_id: int,
        account: RiotId,
        snapshots: dict[RiotGame, RiotStats],
        expected: dict[RiotGame, datetime | None],
    ) -> dict[RiotGame, RiotProfile]:
        with self.lock, self.db.session.begin() as session:
            user = session.get(User, user_id, with_for_update=True)
            if not user or user.banned or user.is_test:
                raise DomainError("Анкета недоступна.")
            current = {game: session.get(RiotProfile, (user_id, game)) for game in RiotGame}
            for game, checked_at in expected.items():
                row = current[game]
                if (row.checked_at if row else None) != checked_at:
                    raise DomainError("Профиль Riot уже изменился. Открой «Добавить игры» ещё раз.")
            saved = {}
            for game, profile in current.items():
                if game not in snapshots:
                    if profile and RiotId(profile.game_name, profile.tag_line).normalized != account.normalized:
                        session.delete(profile)
                    continue
                stats = snapshots[game]
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
                saved[game] = profile
            return saved

    def disconnect(self, user_id: int) -> None:
        with self.lock, self.db.session.begin() as session:
            if session.get(User, user_id, with_for_update=True):
                for game in RiotGame:
                    profile = session.get(RiotProfile, (user_id, game))
                    if profile:
                        session.delete(profile)
