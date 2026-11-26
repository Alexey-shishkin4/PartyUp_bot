from dataclasses import dataclass
from enum import StrEnum

from app.domain.errors import DomainError
from app.domain.games import LEAGUE_GAME_ID, VALORANT_GAME_ID


class RiotGame(StrEnum):
    VALORANT = "valorant"
    LEAGUE = "lol"


RIOT_GAME_IDS = {RiotGame.VALORANT: VALORANT_GAME_ID, RiotGame.LEAGUE: LEAGUE_GAME_ID}


VALORANT_REGIONS = frozenset({"eu", "na", "ap", "kr", "br", "latam"})
LEAGUE_REGIONS = frozenset(
    {"br1", "eun1", "euw1", "jp1", "kr", "la1", "la2", "me1", "na1", "oc1", "ru", "sg2", "tr1", "tw2", "vn2"}
)
LEAGUE_ALIASES = {
    "br": "br1",
    "lan": "la1",
    "las": "la2",
    "na": "na1",
    "jp": "jp1",
    "eune": "eun1",
    "euw": "euw1",
    "me": "me1",
    "tr": "tr1",
    "oce": "oc1",
    "sea": "sg2",
    "ph2": "sg2",
    "th2": "sg2",
}

VALORANT_ACT = "valorant_act"
VALORANT_RIOT_HISTORY = "valorant_riot_history"
VALORANT_RANKED_ACTS = "valorant_ranked_acts"
LEAGUE_SOLO = "RANKED_SOLO_5x5"
LEAGUE_FLEX = "RANKED_FLEX_SR"


@dataclass(frozen=True)
class RiotId:
    name: str
    tag: str

    @property
    def normalized(self) -> tuple[str, str]:
        return self.name.casefold(), self.tag.casefold()

    @classmethod
    def parse(cls, value: str) -> "RiotId":
        name, separator, tag = value.strip().partition("#")
        if (
            not separator
            or not 3 <= len(name) <= 16
            or not name.isprintable()
            or name != name.strip()
            or any(char in name for char in "/\\?")
            or name in {".", ".."}
            or not 3 <= len(tag) <= 5
            or not tag.isalnum()
        ):
            raise DomainError("Укажи Riot ID в формате Имя#Тег.")
        return cls(name, tag)


def normalize_region(game: RiotGame, region: str) -> str:
    region = region.strip().lower()
    if game is RiotGame.VALORANT and region in VALORANT_REGIONS:
        return region
    if game is RiotGame.LEAGUE:
        platform = LEAGUE_ALIASES.get(region, region)
        if platform in LEAGUE_REGIONS:
            return platform
    raise DomainError("Этот сервер игры не поддерживается.")


@dataclass(frozen=True)
class RiotStats:
    riot_id: RiotId
    rank: str | None
    matches: int | None
    scope: str
    source: str
    region: str
