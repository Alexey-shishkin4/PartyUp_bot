from dataclasses import dataclass
from datetime import datetime, timedelta

RECENT_WINDOW = timedelta(days=30)

MODE_LABELS = {
    "battle_royale": "Battle Royale",
    "zero_build": "Zero Build",
    "reload": "Reload",
    "reload_zero_build": "Reload Zero Build",
    "og": "Fortnite OG",
    "og_zero_build": "Fortnite OG Zero Build",
    "ballistic": "Ballistic",
    "blitz": "Blitz",
    "rocket_racing": "Rocket Racing",
    "team:solo": "Соло",
    "team:duo": "Дуо",
    "team:trio": "Трио",
    "team:squad": "Отряд",
    "team:other": "Другие форматы",
}


@dataclass(frozen=True)
class EpicAccount:
    account_id: str
    display_name: str


@dataclass(frozen=True)
class Playtime:
    total_minutes: int | None
    modes: dict[str, int]
    source: str


@dataclass(frozen=True)
class Activity:
    minutes: dict[str, int]
    matches: dict[str, int]

    def favorite_mode(self) -> str | None:
        played = {mode for mode, minutes in self.minutes.items() if minutes > 0}
        played.update(mode for mode, matches in self.matches.items() if matches > 0)
        return min(
            played,
            key=lambda mode: (-self.minutes.get(mode, 0), -self.matches.get(mode, 0), mode),
            default=None,
        )


@dataclass(frozen=True)
class Rank:
    name: str | None
    track_id: str | None
    source: str


@dataclass(frozen=True)
class FortniteSnapshot:
    account: EpicAccount
    checked_at: datetime
    playtime: Playtime | None = None
    activity: Activity | None = None
    ranks: dict[str, Rank] | None = None


def favorite_rank_mode(favorite: str | None, ranks: dict[str, str | None]) -> str | None:
    if favorite and ranks.get(favorite):
        return favorite
    if favorite in {"reload", "reload_zero_build"} and ranks.get("reload_combined"):
        return "reload_combined"
    return None
