from dataclasses import dataclass

from app.domain.errors import DomainError


MODES = ("solo-fpp", "solo", "duo-fpp", "duo", "squad-fpp", "squad")
PLATFORMS = frozenset({"steam", "kakao", "xbox", "psn"})


@dataclass(frozen=True)
class PubgAccount:
    account_id: str
    nickname: str
    platform: str


@dataclass(frozen=True)
class PubgPlaytime:
    mode_seconds: dict[str, int]
    source: str


@dataclass(frozen=True)
class PubgRanking:
    modes: dict[str, str]
    source: str
    season_id: str | None = None


@dataclass(frozen=True)
class PubgStats:
    account: PubgAccount
    playtime: PubgPlaytime | None
    ranking: PubgRanking | None


def parse_account(value: str) -> tuple[str, str]:
    value = value.strip()
    parts = value.split(maxsplit=1)
    platform = "steam"
    if len(parts) == 2 and parts[0].lower() in PLATFORMS:
        platform, value = parts[0].lower(), parts[1]
    if not 1 <= len(value) <= 32 or not value.isprintable() or any(char in value for char in ",/\\"):
        raise DomainError("Укажи ник PUBG. Для другой платформы добавь steam, kakao, xbox или psn перед ником.")
    return platform, value
