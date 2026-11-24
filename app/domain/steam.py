from dataclasses import dataclass, field


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
