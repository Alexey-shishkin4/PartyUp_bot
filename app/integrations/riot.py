from dataclasses import replace
from time import monotonic
from urllib.parse import quote

import httpx

from app.domain.errors import DomainError
from app.domain.riot import (
    LEAGUE_FLEX,
    LEAGUE_SOLO,
    VALORANT_RANKED_ACTS,
    VALORANT_RIOT_HISTORY,
    RiotGame,
    RiotId,
    RiotStats,
    normalize_region,
)

UGI_URL = "https://api.ugilabs.com"
HENRIK_URL = "https://api.henrikdev.xyz"
RIOT_ACCOUNT_URL = "https://europe.api.riotgames.com/riot/account/v1"


class RiotAPIError(Exception):
    def __init__(self, status: int = 0):
        self.status = status


def as_object(value):
    if not isinstance(value, dict):
        raise RiotAPIError()
    return value


def optional_object(value):
    return {} if value is None else as_object(value)


def read_riot_id(payload: dict, name_key: str, tag_key: str) -> RiotId:
    name = payload.get(name_key)
    tag = payload.get(tag_key)
    if not isinstance(name, str) or not isinstance(tag, str):
        raise RiotAPIError()
    try:
        return RiotId.parse(f"{name}#{tag}")
    except DomainError:
        raise RiotAPIError() from None


def read_region(game: RiotGame, value) -> str:
    if not isinstance(value, str):
        raise RiotAPIError()
    try:
        return normalize_region(game, value)
    except DomainError:
        raise RiotAPIError() from None


def text_field(value, limit: int):
    if isinstance(value, str) and 0 < len(value.strip()) <= limit and value.isprintable():
        return value.strip()
    return None


def match_count(value):
    if type(value) is int and 0 <= value <= 2_147_483_647:
        return value
    return None


def ranked_games_by_act(seasons):
    if not isinstance(seasons, list):
        return None
    counts = {}
    for entry in seasons:
        if not isinstance(entry, dict):
            continue
        season = entry.get("season")
        act_id = season.get("id") if isinstance(season, dict) else entry.get("season_id")
        games = match_count(entry.get("games"))
        if isinstance(act_id, str) and act_id.strip() and games is not None:
            act_id = act_id.strip().lower()
            counts[act_id] = max(counts.get(act_id, 0), games)
    return match_count(sum(counts.values())) if counts else None


class RiotClient:
    def __init__(self, http: httpx.AsyncClient, *, ugi_key="", henrik_key="", riot_key=""):
        self.http = http
        self.ugi_key = ugi_key.strip()
        self.henrik_key = henrik_key.strip()
        self.riot_key = riot_key.strip()
        self.cooldowns: dict[str, float] = {}

    async def fetch(self, game: RiotGame, region: str | None, riot_id: RiotId) -> RiotStats:
        if region is not None:
            region = normalize_region(game, region)
        segments = f"{quote(riot_id.name, safe='')}/{quote(riot_id.tag, safe='')}"
        if game is RiotGame.VALORANT:
            if not (self.ugi_key or self.henrik_key):
                raise DomainError("Valorant пока не настроен. Нужен ключ ugiAPI или HenrikDev.")
            if region is None:
                region = await self.valorant_region(segments)
            return await self.valorant(region, segments)
        if not self.riot_key:
            raise DomainError("League of Legends пока не настроена. Нужен ключ Riot Games API.")
        try:
            return await self.league(region, segments)
        except RiotAPIError as error:
            raise self.provider_error("League of Legends", [error]) from None

    async def get(self, provider: str, url: str, headers: dict[str, str]):
        if monotonic() < self.cooldowns.get(provider, 0):
            raise RiotAPIError(429)
        try:
            response = await self.http.get(url, headers=headers, follow_redirects=False, timeout=8)
            if response.status_code == 429:
                retry_after = response.headers.get("Retry-After", "")
                delay = max(1, int(retry_after)) if retry_after.isascii() and retry_after.isdecimal() else 60
                self.cooldowns[provider] = monotonic() + delay
            if not response.is_success:
                raise RiotAPIError(response.status_code)
            return response.json()
        except (httpx.RequestError, ValueError):
            raise RiotAPIError() from None

    @staticmethod
    def provider_error(game: str, errors: list[RiotAPIError]) -> DomainError:
        statuses = {error.status for error in errors}
        if statuses == {404}:
            return DomainError(f"Riot ID не найден в {game}. Проверь имя и тег.")
        if statuses <= {401, 403}:
            return DomainError(f"Для {game} нужно проверить ключ API. Сообщи администратору.")
        if 429 in statuses:
            return DomainError(f"Лимит запросов к API {game} исчерпан. Попробуй позже.")
        return DomainError(f"Статистику {game} временно не удалось получить. Попробуй позже.")

    async def valorant_region(self, segments: str) -> str:
        if not (self.henrik_key or self.riot_key):
            raise DomainError("Автоопределение сервера Valorant пока не настроено. Сообщи администратору.")
        errors = []
        if self.henrik_key:
            try:
                payload = as_object(
                    await self.get(
                        "henrik",
                        f"{HENRIK_URL}/valorant/v2/account/{segments}",
                        {"Authorization": self.henrik_key},
                    )
                )
                return read_region(RiotGame.VALORANT, as_object(payload.get("data")).get("region"))
            except RiotAPIError as error:
                errors.append(error)
        if self.riot_key:
            try:
                _, puuid = await self.riot_account(segments)
                shard = as_object(
                    await self.riot_get(f"{RIOT_ACCOUNT_URL}/active-shards/by-game/val/by-puuid/{puuid}")
                )
                return read_region(RiotGame.VALORANT, shard.get("activeShard"))
            except RiotAPIError as error:
                errors.append(error)
        raise self.provider_error("Valorant", errors)

    async def valorant(self, region: str, segments: str) -> RiotStats:
        errors = []
        snapshots = []
        rank = None
        for api_key, load in (
            (self.ugi_key, self.ugi_history),
            (self.ugi_key, self.ugi_rank),
            (self.henrik_key, self.henrik),
        ):
            if not api_key:
                continue
            try:
                stats = await load(region, segments)
            except RiotAPIError as error:
                errors.append(error)
                continue
            if stats.rank is None and stats.matches is None:
                errors.append(RiotAPIError())
                continue
            snapshots.append(stats)
            if rank is None and stats.rank is not None:
                rank = stats
            if rank is not None and any(
                snapshot.matches is not None and snapshot.riot_id.normalized == rank.riot_id.normalized
                for snapshot in snapshots
            ):
                break
        if not snapshots:
            raise self.provider_error("Valorant", errors)
        account = (rank or snapshots[0]).riot_id
        counts = [
            stats
            for stats in snapshots
            if stats.matches is not None and stats.riot_id.normalized == account.normalized
        ]
        if not counts:
            return rank or snapshots[0]
        # Riot's available all-queue history can be shorter than the ranked history.
        best = max(counts, key=lambda stats: (stats.matches, stats.scope == VALORANT_RIOT_HISTORY))
        if rank is None or rank.source == best.source:
            return replace(best, rank=rank.rank if rank else None)
        return replace(best, rank=rank.rank, source=f"{rank.source}_{best.source}")

    async def ugi_history(self, region: str, segments: str) -> RiotStats:
        payload = as_object(
            await self.get(
                "ugi",
                f"{UGI_URL}/v1/matches/{region}/{segments}?size=1",
                {"Authorization": f"Bearer {self.ugi_key}"},
            )
        )
        data = as_object(payload.get("data"))
        if data.get("mode") is not None:
            raise RiotAPIError()
        account = read_riot_id(data, "name", "tag")
        pagination = as_object(data.get("pagination"))
        return RiotStats(
            account, None, match_count(pagination.get("total")), VALORANT_RIOT_HISTORY, "ugi", region
        )

    async def ugi_rank(self, region: str, segments: str) -> RiotStats:
        payload = as_object(
            await self.get(
                "ugi",
                f"{UGI_URL}/v1/rank/{region}/{segments}",
                {"Authorization": f"Bearer {self.ugi_key}"},
            )
        )
        data = as_object(payload.get("data"))
        account = read_riot_id(data, "name", "tag")
        current = optional_object(data.get("current_data"))
        return RiotStats(
            account,
            text_field(current.get("currenttierpatched"), 48),
            ranked_games_by_act(data.get("by_season")),
            VALORANT_RANKED_ACTS,
            "ugi",
            region,
        )

    async def henrik(self, region: str, segments: str) -> RiotStats:
        payload = as_object(
            await self.get(
                "henrik",
                f"{HENRIK_URL}/valorant/v3/mmr/{region}/pc/{segments}",
                {"Authorization": self.henrik_key},
            )
        )
        data = as_object(payload.get("data"))
        account = read_riot_id(as_object(data.get("account")), "name", "tag")
        current = optional_object(optional_object(data.get("current")).get("tier"))
        rank = text_field(current.get("name"), 48)
        matches = ranked_games_by_act(data.get("seasonal"))
        return RiotStats(account, rank, matches, VALORANT_RANKED_ACTS, "henrik", region)

    async def riot_get(self, url: str):
        return await self.get("riot", url, {"X-Riot-Token": self.riot_key})

    async def riot_account(self, segments: str) -> tuple[RiotId, str]:
        account = as_object(await self.riot_get(f"{RIOT_ACCOUNT_URL}/accounts/by-riot-id/{segments}"))
        riot_id = read_riot_id(account, "gameName", "tagLine")
        puuid = text_field(account.get("puuid"), 128)
        if not puuid or not puuid.isascii() or not puuid.replace("-", "").replace("_", "").isalnum():
            raise RiotAPIError()
        return riot_id, puuid

    async def league(self, region: str | None, segments: str) -> RiotStats:
        riot_id, puuid = await self.riot_account(segments)
        if region is None:
            account_region = as_object(
                await self.riot_get(f"{RIOT_ACCOUNT_URL}/region/by-game/lol/by-puuid/{puuid}")
            )
            region = read_region(RiotGame.LEAGUE, account_region.get("region"))
        platform_url = f"https://{region}.api.riotgames.com"
        data = await self.riot_get(f"{platform_url}/lol/league/v4/entries/by-puuid/{puuid}")
        if not isinstance(data, list):
            raise RiotAPIError()
        queues = {}
        for item in data:
            entry = as_object(item)
            if entry.get("queueType") in (LEAGUE_SOLO, LEAGUE_FLEX):
                queues[entry["queueType"]] = entry
        for queue in (LEAGUE_SOLO, LEAGUE_FLEX):
            if queue in queues:
                entry = queues[queue]
                tier = text_field(entry.get("tier"), 24)
                division = text_field(entry.get("rank"), 8)
                wins, losses = match_count(entry.get("wins")), match_count(entry.get("losses"))
                if not tier or wins is None or losses is None or match_count(wins + losses) is None:
                    raise RiotAPIError()
                rank = tier.title()
                if division and tier not in {"MASTER", "GRANDMASTER", "CHALLENGER"}:
                    rank = f"{rank} {division}"
                return RiotStats(riot_id, rank, wins + losses, queue, "riot", region)
        # Account-V1 is global. An empty list alone cannot confirm the LoL server.
        await self.riot_get(f"{platform_url}/lol/summoner/v4/summoners/by-puuid/{puuid}")
        return RiotStats(riot_id, None, None, LEAGUE_SOLO, "riot", region)
