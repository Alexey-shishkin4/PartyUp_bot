import re
from dataclasses import dataclass
from typing import Literal
from urllib.parse import unquote, urlsplit

from app.domain.errors import DomainError


NICKNAME = re.compile(r"[A-Za-z0-9_-]{3,32}")
STEAM_ID = re.compile(r"[0-9]{17}")
PROFILE_PATH = re.compile(r"/(?:[a-z]{2}/)?players/([^/]+)/?", re.IGNORECASE)


@dataclass(frozen=True)
class FaceitLookup:
    kind: Literal["nickname", "steam_id", "player_id"]
    value: str

    @classmethod
    def parse(cls, value: str) -> "FaceitLookup":
        value = value.strip()
        if STEAM_ID.fullmatch(value):
            return cls("steam_id", value)
        if NICKNAME.fullmatch(value):
            return cls("nickname", value)
        try:
            url = urlsplit(value)
        except ValueError:
            url = None
        if url and url.scheme in {"http", "https"} and url.netloc.lower() in {"faceit.com", "www.faceit.com"}:
            match = PROFILE_PATH.fullmatch(url.path)
            if match:
                nickname = unquote(match[1])
                if NICKNAME.fullmatch(nickname):
                    return cls("nickname", nickname)
        raise DomainError("Отправь никнейм FACEIT, ссылку на профиль FACEIT или SteamID64.")


@dataclass(frozen=True)
class FaceitStats:
    player_id: str
    nickname: str
    skill_level: int | None
    elo: int | None
    match_count: int | None
    wins: int | None
    win_rate: float | None
