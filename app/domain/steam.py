from dataclasses import dataclass, field


STEAM_INDIVIDUAL_BASE = 76561197960265728
CS2_APP_ID = 730
PUBG_APP_ID = 578080
DEADLOCK_APP_ID = 1422450


@dataclass
class PublicProfile:
    steam_id: str
    url: str
    games: list[dict] = field(default_factory=list)
    coverage: str = "profile_only"
    warning: str = ""
    dota_rank_tier: int | None = None
    dota_rank_checked: bool = False
    dota_match_minutes: int | None = None
