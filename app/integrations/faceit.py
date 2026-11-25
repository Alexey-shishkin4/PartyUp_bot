import re
from time import monotonic
from uuid import UUID

import httpx

from app.domain.errors import DomainError
from app.domain.faceit import NICKNAME, STEAM_ID, FaceitLookup, FaceitStats


BASE_URL = "https://open.faceit.com/data/v4"
WOK_URL = "https://woksteamapi.com/faceit/data/v4"
GAME = "cs2"
UNAVAILABLE = "Статистика FACEIT временно недоступна. Попробуй позже."
RATE_LIMIT = "FACEIT ограничил запросы. Попробуй обновить статистику позже."


def integer(value, *, minimum=0, maximum=2_147_483_647) -> int | None:
    if isinstance(value, str) and re.fullmatch(r"[0-9]{1,10}", value):
        value = int(value)
    return value if type(value) is int and minimum <= value <= maximum else None


def percent(value) -> float | None:
    if isinstance(value, str) and re.fullmatch(r"[0-9]{1,3}(?:\.[0-9]+)?%?", value):
        value = float(value.rstrip("%"))
    return float(value) if type(value) in (int, float) and 0 <= value <= 100 else None


class FaceitClient:
    def __init__(self, http: httpx.AsyncClient, api_key: str, *, wok_api_key: str = ""):
        self.http = http
        faceit_key = api_key.strip()
        wok_key = wok_api_key.strip()
        self.uses_wok = not faceit_key and bool(wok_key)
        self.api_key = faceit_key or wok_key
        self.base_url = WOK_URL if self.uses_wok else BASE_URL
        self.cooldown_until = 0.0

    async def fetch(self, lookup: FaceitLookup) -> FaceitStats:
        if not self.api_key:
            raise DomainError("Интеграция FACEIT пока не настроена. Сообщи администратору бота.")

        expected_id = None
        if lookup.kind == "player_id":
            try:
                expected_id = str(UUID(lookup.value))
            except ValueError:
                raise DomainError(UNAVAILABLE) from None
        steam_id = lookup.value if lookup.kind == "steam_id" else None
        if self.uses_wok:
            if expected_id is not None:
                steam_id = lookup.steam_id
            if steam_id is None or not STEAM_ID.fullmatch(steam_id):
                raise DomainError("WOK ищет профиль по SteamID64. Укажи его или выбери подключённый Steam.")
        if steam_id is not None:
            player = await self.get("/players", params={"game": GAME, "game_player_id": steam_id})
        elif expected_id is not None:
            player = await self.get(f"/players/{expected_id}")
        else:
            player = await self.get("/players", params={"nickname": lookup.value})
        if player is None:
            raise DomainError("Профиль FACEIT не найден. Проверь никнейм или SteamID64.")

        nickname = player.get("nickname")
        if not isinstance(nickname, str) or not NICKNAME.fullmatch(nickname):
            raise DomainError(UNAVAILABLE)
        try:
            player_id = str(UUID(player["player_id"]))
        except (KeyError, AttributeError, TypeError, ValueError):
            raise DomainError(UNAVAILABLE) from None
        if expected_id is not None and player_id != expected_id:
            if self.uses_wok:
                raise DomainError("Этот SteamID64 связан с другим профилем FACEIT. Проверь данные или смени профиль.")
            raise DomainError(UNAVAILABLE)
        if lookup.kind == "nickname" and nickname.casefold() != lookup.value.casefold():
            raise DomainError("Профиль FACEIT не найден. Проверь никнейм.")

        games = player.get("games", {})
        if not isinstance(games, dict):
            raise DomainError(UNAVAILABLE)
        cs2 = games.get(GAME)
        if cs2 is None:
            raise DomainError("В этом профиле FACEIT нет подключённой игры Counter-Strike 2.")
        if not isinstance(cs2, dict):
            raise DomainError(UNAVAILABLE)
        returned_steam_id = cs2.get("game_player_id")
        if steam_id is not None and str(returned_steam_id) != steam_id:
            # WOK's compatible response may omit game_player_id after a lookup by SteamID64.
            if not self.uses_wok or returned_steam_id is not None:
                raise DomainError("Профиль FACEIT для этого SteamID64 не найден.")
        if steam_id is None and returned_steam_id is not None and STEAM_ID.fullmatch(str(returned_steam_id)):
            steam_id = str(returned_steam_id)

        stats = await self.get(f"/players/{player_id}/stats/{GAME}")
        if stats is not None and (stats.get("game_id", GAME) != GAME or stats.get("player_id", player_id) != player_id):
            raise DomainError(UNAVAILABLE)
        lifetime = stats.get("lifetime") if stats is not None else None
        if lifetime is None:
            lifetime = {}
        if not isinstance(lifetime, dict):
            raise DomainError(UNAVAILABLE)
        matches = integer(lifetime.get("Matches"))
        wins = integer(lifetime.get("Wins"), maximum=matches if matches is not None else 2_147_483_647)
        win_rate = percent(lifetime.get("Win Rate %"))
        if win_rate is None and matches and wins is not None:
            win_rate = round(100 * wins / matches, 2)
        return FaceitStats(
            player_id,
            nickname,
            integer(cs2.get("skill_level"), minimum=1, maximum=10),
            integer(cs2.get("faceit_elo")),
            matches,
            wins,
            win_rate,
            steam_id=steam_id,
            source="wok" if self.uses_wok else "faceit",
        )

    async def get(self, path: str, *, params=None) -> dict | None:
        rate_limit = "WOK ограничил запросы. Попробуй обновить статистику позже." if self.uses_wok else RATE_LIMIT
        if monotonic() < self.cooldown_until:
            raise DomainError(rate_limit)
        try:
            response = await self.http.get(
                self.base_url + path,
                params=params,
                headers={"Authorization": f"Bearer {self.api_key}", "Accept": "application/json"},
                follow_redirects=False,
                timeout=8,
            )
            if response.status_code == 404:
                return None
            if response.status_code in {401, 403}:
                if self.uses_wok:
                    raise DomainError("WOK отклонил ключ API. Сообщи администратору бота.")
                raise DomainError("FACEIT отклонил ключ API. Сообщи администратору бота.")
            retry_after = integer(response.headers.get("Retry-After"), minimum=1)
            if self.uses_wok and response.status_code in {402, 503} and retry_after:
                self.cooldown_until = max(self.cooldown_until, monotonic() + retry_after)
            if self.uses_wok and response.status_code == 402:
                raise DomainError("Квота WOK исчерпана. Попробуй позже.")
            if response.status_code == 429:
                self.cooldown_until = max(self.cooldown_until, monotonic() + (retry_after or 60))
                raise DomainError(rate_limit)
            response.raise_for_status()
            data = response.json()
        except (httpx.HTTPError, ValueError):
            raise DomainError(UNAVAILABLE) from None
        if not isinstance(data, dict):
            raise DomainError(UNAVAILABLE)
        return data
