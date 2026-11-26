from app.domain.errors import DomainError
from app.domain.riot import RIOT_GAME_IDS
from app.persistence.game_stats import find_stats, replace_account, save_stats, writable_user
from app.persistence.models import utcnow

class RiotProfiles:
    def __init__(self, db, lock):
        self.db = db
        self.lock = lock

    def get(self, user_id, game):
        with self.db.session() as session:
            return find_stats(session, user_id, "riot", RIOT_GAME_IDS[game])

    def save_account(self, user_id, identity, snapshots, expected):
        with self.lock, self.db.session.begin() as session:
            user = writable_user(session, user_id)
            current = {
                game: find_stats(session, user_id, "riot", game_id) for game, game_id in RIOT_GAME_IDS.items()
            }
            for game, checked_at in expected.items():
                row = current[game]
                if (row.checked_at if row else None) != checked_at:
                    raise DomainError("Профиль Riot уже изменился. Открой «Добавить игры» ещё раз.")
            self.store(session, user, identity, snapshots, checked_at=utcnow(), current=current)

    @staticmethod
    def store(session, user, identity, snapshots, *, checked_at, current=None):
        account = replace_account(session, user, "riot", "#".join(identity.normalized))
        account.display_name = f"{identity.name}#{identity.tag}"
        account.checked_at = checked_at
        for game, stats in snapshots.items():
            row = save_stats(session, account, RIOT_GAME_IDS[game], current.get(game) if current else None)
            row.rank = stats.rank
            row.matches_count = stats.matches
            row.scope = stats.scope
            row.source = stats.source
            row.checked_at = checked_at
            row.details = {"region": stats.region}

    def disconnect(self, user_id):
        with self.lock, self.db.session.begin() as session:
            user = writable_user(session, user_id)
            account = user.game_account("riot")
            if account:
                user.accounts.remove(account)
