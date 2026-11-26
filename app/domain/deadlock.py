from dataclasses import dataclass
from datetime import datetime


RANKS = (
    "Obscurus", "Initiate", "Seeker", "Acolyte", "Sentinel", "Mystic", "Ritualist",
    "Emissary", "Oracle", "Phantom", "Ascendant", "Eternus",
)


def rank_name(badge: int) -> str | None:
    tier, subrank = divmod(badge, 10)
    if not 0 < tier < len(RANKS) or not 0 <= subrank <= 6:
        return None
    return f"{RANKS[tier]} {subrank}" if subrank else RANKS[tier]


@dataclass(frozen=True)
class DeadlockStats:
    rank_badge: int
    ranked_at: datetime | None
    match_count: int | None = None
    match_seconds: int | None = None
