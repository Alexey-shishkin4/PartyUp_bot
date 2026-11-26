import asyncio

import httpx

from app.domain.dota import DOTA_APP_ID
from app.domain.errors import DomainError
from app.domain.steam import PublicProfile

from .opendota import OpenDotaClient
from .steam_public import ID_PATTERN, normalize_profile_url


class SteamClient:
    def __init__(self, api_key: str, client: httpx.AsyncClient, dota: OpenDotaClient | None = None):
        self.api_key, self.client = api_key, client
        self.dota = dota

    async def owned_games(self, steam_id: str):
        if not self.api_key:
            raise DomainError("Для Steam Web API нужен STEAM_API_KEY")
        payload = await self._web_api(
            "IPlayerService/GetOwnedGames", steamid=steam_id, include_appinfo=1, include_played_free_games=1
        )
        if "game_count" not in payload:
            raise DomainError(
                "Библиотека скрыта или недоступна. Откройте «Доступ к игровой информации» в Steam или выберите игры вручную"
            )
        games = payload.get("games", [])
        if not isinstance(games, list) or payload["game_count"] != len(games):
            raise DomainError("Steam вернул неполную библиотеку. Сохранённые игры остаются доступны")
        return self._normalize_games(games)

    async def _recent_games(self, steam_id: str):
        if not self.api_key:
            return []
        try:
            payload = await self._web_api("IPlayerService/GetRecentlyPlayedGames", steamid=steam_id)
            games = payload.get("games", [])
            if isinstance(games, list):
                return self._normalize_games(games)
        except DomainError:
            pass
        return []

    @staticmethod
    def _normalize_games(games):
        result = []
        for row in games:
            app_id = row.get("appid") if isinstance(row, dict) else None
            if type(app_id) is not int or not 0 < app_id < 2**32:
                raise DomainError("Steam вернул некорректную библиотеку. Сохранённые игры остаются доступны")
            name = row.get("name")
            game = {
                "appid": app_id,
                "name": name.strip()[:160] if isinstance(name, str) and name.strip() else f"Steam {app_id}",
            }
            for field in ("playtime_forever", "playtime_2weeks"):
                value = row.get(field, 0)
                if type(value) is not int or not 0 <= value < 2**32:
                    raise DomainError("Steam вернул некорректное игровое время")
                game[field] = value
            result.append(game)
        return result

    async def game_profile(self, steam_id: str) -> PublicProfile:
        owned, recent = await asyncio.gather(
            self.owned_games(steam_id),
            self._recent_games(steam_id),
            return_exceptions=True,
        )
        if isinstance(recent, BaseException):
            raise recent
        url = f"https://steamcommunity.com/profiles/{steam_id}"
        if isinstance(owned, BaseException):
            if not recent or not isinstance(owned, DomainError):
                raise owned
            return await self._include_dota(PublicProfile(steam_id, url, games=recent, coverage="recent"))

        # Steam may report a played free game only in its recent activity response.
        games = {game["appid"]: game for game in owned}
        for game in recent:
            entry = games.setdefault(game["appid"], game)
            entry["playtime_forever"] = max(entry["playtime_forever"], game["playtime_forever"])
            entry["playtime_2weeks"] = max(entry["playtime_2weeks"], game["playtime_2weeks"])
        return await self._include_dota(
            PublicProfile(steam_id, url, games=list(games.values()), coverage="library")
        )

    async def _include_dota(self, profile: PublicProfile) -> PublicProfile:
        game = next((game for game in profile.games if game["appid"] == DOTA_APP_ID), None)
        if self.dota is not None and game and (game.get("playtime_forever", 0) or game.get("playtime_2weeks", 0)):
            stats = await self.dota.stats(profile.steam_id, need_duration=not game.get("playtime_forever"))
            profile.dota_rank_tier = stats.rank_tier
            profile.dota_rank_checked = stats.rank_checked
            profile.dota_match_minutes = stats.match_minutes
        return profile

    async def _web_api(self, method, **params):
        try:
            response = await self.client.get(
                f"https://api.steampowered.com/{method}/v1/", params={"key": self.api_key, **params}
            )
            response.raise_for_status()
            payload = response.json()["response"]
            if isinstance(payload, dict):
                return payload
        except (httpx.HTTPError, ValueError, TypeError, KeyError):
            pass
        raise DomainError("Не удалось получить данные Steam. Сохранённые игры остаются доступны")

    async def fetch_profile(self, value, public_steam):
        url = normalize_profile_url(value)
        if self.api_key:
            try:
                kind, identifier = url.rsplit("/", 2)[-2:]
                steam_id = identifier
                if kind == "id":
                    resolved = await self._web_api("ISteamUser/ResolveVanityURL", vanityurl=identifier)
                    steam_id = resolved.get("steamid", "")
                    if (
                        resolved.get("success") != 1
                        or not isinstance(steam_id, str)
                        or not ID_PATTERN.fullmatch(steam_id)
                    ):
                        raise DomainError("Профиль Steam не найден. Проверь ссылку")
                return await self.game_profile(steam_id)
            except DomainError:
                if public_steam is None:
                    raise
        if public_steam is None:
            raise DomainError("Парсер Steam временно недоступен")
        return await self._include_dota(await public_steam.fetch(url))
