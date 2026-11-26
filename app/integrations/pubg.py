import asyncio
import logging
import re
from collections import deque
from math import isfinite
from time import monotonic, time

import httpx

from app.domain.errors import DomainError
from app.domain.pubg import MODES, PLATFORMS, PubgAccount, PubgPlaytime, PubgRanking, PubgStats


OFFICIAL = "https://api.pubg.com/shards"
PUBLIC = "https://pubg-api.org/api"
PUBLIC_PLATFORMS = {"steam", "kakao"}
ACCOUNT_ID = re.compile(r"account\.[0-9a-fA-F]{32}")
SEASON_ID = re.compile(r"division\.bro\.official\.[a-z0-9-]{1,64}")
log = logging.getLogger(__name__)


class PubgAPIError(Exception):
    def __init__(self, status: int = 0):
        self.status = status


def object_value(value) -> dict:
    if not isinstance(value, dict):
        raise PubgAPIError()
    return value


def lifetime_seconds(payload, account_id: str) -> dict[str, int]:
    data = object_value(payload.get("data"))
    relationships = data.get("relationships")
    if isinstance(relationships, dict) and "player" in relationships:
        player = object_value(object_value(relationships["player"]).get("data"))
        if player.get("id") != account_id:
            raise PubgAPIError()
    modes = object_value(object_value(data.get("attributes")).get("gameModeStats"))
    seconds = {}
    for mode in MODES:
        entry = modes.get(mode)
        duration = entry.get("timeSurvived") if isinstance(entry, dict) else None
        if type(duration) in (int, float) and 0 <= duration < 2**53 and isfinite(duration):
            seconds[mode] = int(duration)
    if not seconds:
        raise PubgAPIError()
    return seconds


def current_ranks(modes) -> dict[str, str]:
    modes = object_value(modes)
    ranks = {}
    for mode in MODES:
        if mode not in modes:
            continue
        entry = object_value(modes[mode])
        tier = entry.get("currentTier")
        if tier is None:
            continue
        name = object_value(tier).get("tier")
        if name is None or name == "":
            continue
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 -]{0,23}", name):
            raise PubgAPIError()
        if name.casefold() in {"unranked", "none"}:
            continue
        division = str(tier.get("subTier", ""))
        rank = f"{name} {division}" if division in {"1", "2", "3", "4", "5"} else name
        rp = entry.get("currentRankPoint")
        ranks[mode] = f"{rank} · {rp} RP" if type(rp) is int and rp > 0 else rank
    return ranks


class PubgClient:
    def __init__(self, http: httpx.AsyncClient, api_key: str = "", *, requests_per_minute: int = 10):
        self.http = http
        self.api_key = api_key.strip().removeprefix("Bearer ").strip()
        self.requests_per_minute = requests_per_minute
        self.official_requests: deque[float] = deque()
        self.cooldowns: dict[str, float] = {}
        self.seasons: dict[str, tuple[float, str]] = {}
        self.season_lock = asyncio.Lock()

    async def fetch(
        self, platform: str, nickname: str, *, account_id: str | None = None,
        need_playtime: bool = True, need_rank: bool = True,
    ) -> PubgStats:
        if platform not in PLATFORMS:
            raise DomainError("Платформа PUBG не поддерживается.")
        if platform not in PUBLIC_PLATFORMS and not self.api_key:
            raise DomainError("Для PUBG на Xbox и PlayStation нужен ключ официального PUBG API.")
        try:
            account = PubgAccount(account_id, nickname, platform) if account_id else await self.player(platform, nickname)
            if not ACCOUNT_ID.fullmatch(account.account_id):
                raise PubgAPIError()
        except PubgAPIError as error:
            raise self.user_error([error]) from None

        jobs = {}
        if need_playtime:
            jobs["playtime"] = self.playtime(account)
        if need_rank:
            jobs["ranking"] = self.ranking(account)
        results = await asyncio.gather(*jobs.values(), return_exceptions=True)
        stats = {}
        errors = []
        for section, result in zip(jobs, results):
            if isinstance(result, PubgAPIError):
                errors.append(result)
            elif isinstance(result, BaseException):
                raise result
            else:
                stats[section] = result
        if errors and not stats:
            raise self.user_error(errors)
        return PubgStats(account, stats.get("playtime"), stats.get("ranking"))

    async def player(self, platform: str, nickname: str) -> PubgAccount:
        if platform in PUBLIC_PLATFORMS:
            try:
                player = await self.get("public", "/players", {"platform": platform, "playerName": nickname})
                return self.player_identity(player, platform, nickname)
            except PubgAPIError:
                if not self.api_key:
                    raise
        payload = await self.get("official", f"/{platform}/players", {"filter[playerNames]": nickname})
        data = payload.get("data")
        if not isinstance(data, list):
            raise PubgAPIError()
        for player in data:
            try:
                return self.player_identity(player, platform, nickname)
            except PubgAPIError as error:
                if error.status != 404:
                    raise
        raise PubgAPIError(404)

    @staticmethod
    def player_identity(payload, platform: str, nickname: str) -> PubgAccount:
        player = object_value(payload)
        attrs = object_value(player.get("attributes"))
        name, account_id = attrs.get("name"), player.get("id")
        if not isinstance(name, str) or name.casefold() != nickname.casefold():
            raise PubgAPIError(404)
        if not isinstance(account_id, str) or not ACCOUNT_ID.fullmatch(account_id):
            raise PubgAPIError()
        return PubgAccount(account_id, name, platform)

    async def playtime(self, account: PubgAccount) -> PubgPlaytime:
        if account.platform in PUBLIC_PLATFORMS:
            try:
                payload = await self.get(
                    "public", "/lifetime/stats", {"platform": account.platform, "accountId": account.account_id}
                )
                return PubgPlaytime(lifetime_seconds(payload, account.account_id), "public")
            except PubgAPIError:
                if not self.api_key:
                    raise
        payload = await self.get(
            "official", f"/{account.platform}/players/{account.account_id}/seasons/lifetime"
        )
        return PubgPlaytime(lifetime_seconds(payload, account.account_id), "official")

    async def ranking(self, account: PubgAccount) -> PubgRanking:
        if self.api_key:
            try:
                season = await self.current_season(account.platform)
                payload = await self.get(
                    "official", f"/{account.platform}/players/{account.account_id}/seasons/{season}/ranked"
                )
                attrs = object_value(object_value(payload.get("data")).get("attributes"))
                return PubgRanking(current_ranks(attrs.get("rankedGameModeStats")), "official", season)
            except PubgAPIError:
                if account.platform not in PUBLIC_PLATFORMS:
                    raise
        payload = await self.get(
            "public", "/stats/rank", {"platform": account.platform, "playerName": account.nickname}
        )
        if not (payload.keys() & set(MODES) or isinstance(payload.get("banType"), str)):
            raise PubgAPIError()
        return PubgRanking(current_ranks(payload), "public")

    async def current_season(self, platform: str) -> str:
        async with self.season_lock:
            cached = self.seasons.get(platform)
            if cached and monotonic() < cached[0]:
                return cached[1]
            payload = await self.get("official", f"/{platform}/seasons")
            seasons = payload.get("data")
            if not isinstance(seasons, list):
                raise PubgAPIError()
            for row in seasons:
                season = object_value(row)
                if object_value(season.get("attributes")).get("isCurrentSeason") is True:
                    season_id = season.get("id")
                    if isinstance(season_id, str) and SEASON_ID.fullmatch(season_id):
                        self.seasons[platform] = (monotonic() + 3600, season_id)
                        return season_id
            raise PubgAPIError()

    async def get(self, provider: str, path: str, params=None) -> dict:
        now = monotonic()
        if now < self.cooldowns.get(provider, 0):
            raise PubgAPIError(429)
        if provider == "official":
            while self.official_requests and now - self.official_requests[0] >= 60:
                self.official_requests.popleft()
            if len(self.official_requests) >= self.requests_per_minute:
                raise PubgAPIError(429)
            self.official_requests.append(now)
            url = OFFICIAL + path
            headers = {"Authorization": f"Bearer {self.api_key}", "Accept": "application/vnd.api+json"}
        else:
            url = PUBLIC + path
            headers = {"Accept": "application/json"}
        try:
            response = await self.http.get(url, params=params, headers=headers, follow_redirects=False, timeout=8)
            if response.status_code == 429 or (
                provider == "official" and response.headers.get("X-RateLimit-Remaining") == "0"
            ):
                retry = response.headers.get("Retry-After", "")
                reset = response.headers.get("X-RateLimit-Reset", "")
                delay = int(retry) if re.fullmatch(r"[0-9]{1,8}", retry) else 60
                if provider == "official" and re.fullmatch(r"[0-9]{1,12}", reset):
                    delay = max(1, int(reset) - time())
                self.cooldowns[provider] = max(self.cooldowns.get(provider, 0), monotonic() + max(1, delay))
            response.raise_for_status()
            return object_value(response.json())
        except httpx.HTTPStatusError as error:
            log.info("PUBG %s returned HTTP %s", provider, error.response.status_code)
            raise PubgAPIError(error.response.status_code) from None
        except (httpx.RequestError, ValueError) as error:
            log.info("PUBG %s request failed (%s)", provider, type(error).__name__)
            raise PubgAPIError() from None

    @staticmethod
    def user_error(errors: list[PubgAPIError]) -> DomainError:
        statuses = {error.status for error in errors}
        if statuses == {404}:
            return DomainError("PUBG не нашёл профиль или статистику. Проверь ник и платформу.")
        if statuses <= {401, 403}:
            return DomainError("PUBG API отклонил запрос. Проверь ключ у администратора.")
        if 429 in statuses:
            return DomainError("Достигнут лимит PUBG API. Попробуй обновить статистику через минуту.")
        return DomainError("Статистика PUBG временно недоступна. Попробуй позже.")
