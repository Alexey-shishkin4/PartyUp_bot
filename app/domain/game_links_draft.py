from dataclasses import dataclass
from datetime import datetime
from typing import Generic, TypeVar

from pydantic import BaseModel

from app.domain.deadlock import DeadlockStats, rank_name
from app.domain.faceit import FaceitStats
from app.domain.fortnite import FortniteSnapshot, favorite_rank_mode
from app.domain.games import FORTNITE_GAME_ID
from app.domain.pubg import PubgStats, profile_rank
from app.domain.riot import RIOT_GAME_IDS, RiotGame, RiotId, RiotStats
from app.domain.steam import CS2_APP_ID, DEADLOCK_APP_ID, PUBG_APP_ID

Snapshot = TypeVar("Snapshot")


@dataclass(frozen=True)
class CheckedSnapshot(Generic[Snapshot]):
    data: Snapshot
    checked_at: datetime


@dataclass(frozen=True)
class RiotLinkDraft:
    identity: RiotId
    snapshots: dict[RiotGame, RiotStats]
    checked_at: datetime


class GameLinksDraft(BaseModel):
    fortnite: FortniteSnapshot | None = None
    riot: RiotLinkDraft | None = None
    pubg: CheckedSnapshot[PubgStats] | None = None
    faceit: CheckedSnapshot[FaceitStats] | None = None
    deadlock: CheckedSnapshot[DeadlockStats] | None = None

    @classmethod
    def from_state(cls, data):
        return cls.model_validate(data.get("draft_game_links", {}))

    @property
    def has_links(self):
        return any((self.riot, self.fortnite, self.pubg, self.faceit, self.deadlock))

    def clear_steam_links(self, steam_id):
        self.deadlock = None
        if self.faceit and self.faceit.data.steam_id == steam_id:
            self.faceit = None

    def game_ranks(self):
        ranks = {}
        if self.riot is not None:
            ranks.update({RIOT_GAME_IDS[game]: stats.rank for game, stats in self.riot.snapshots.items()})
        if self.fortnite is not None:
            favorite = self.fortnite.activity.favorite_mode() if self.fortnite.activity else None
            mode_ranks = {mode: rank.name for mode, rank in (self.fortnite.ranks or {}).items()}
            ranks[FORTNITE_GAME_ID] = mode_ranks.get(favorite_rank_mode(favorite, mode_ranks))
        if self.pubg is not None:
            stats = self.pubg.data
            ranks[PUBG_APP_ID] = profile_rank(
                stats.ranking.modes if stats.ranking else {}, stats.playtime.mode_seconds if stats.playtime else {},
            )
        if self.faceit is not None:
            level = self.faceit.data.skill_level
            ranks[CS2_APP_ID] = f"FACEIT {level}" if level is not None else None
        if self.deadlock is not None:
            ranks[DEADLOCK_APP_ID] = rank_name(self.deadlock.data.rank_badge)
        return ranks
