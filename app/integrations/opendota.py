import asyncio
from dataclasses import dataclass

import httpx

STEAM_INDIVIDUAL_BASE = 76561197960265728
MATCHMAKING_MINUTES = 7
PLAYTIME_MULTIPLIER = 1.5


@dataclass
class DotaStats:
    rank_tier: int | None = None
    rank_checked: bool = False
    match_minutes: int | None = None


class OpenDotaClient:
    def __init__(self, client: httpx.AsyncClient):
        self.client = client

    async def _get(self, account_id, suffix=""):
        try:
            response = await self.client.get(
                f"https://api.opendota.com/api/players/{account_id}{suffix}",
                timeout=6,
                follow_redirects=False,
            )
            response.raise_for_status()
            return response.json()
        except (httpx.HTTPError, ValueError):
            return None

    async def stats(self, steam_id: str, *, need_duration: bool) -> DotaStats:
        account_id = int(steam_id) - STEAM_INDIVIDUAL_BASE
        if not 0 < account_id < 2**32:
            return DotaStats()
        if need_duration:
            profile, totals = await asyncio.gather(self._get(account_id), self._get(account_id, "/totals"))
        else:
            profile = await self._get(account_id)
            totals = None
        result = DotaStats()
        if isinstance(profile, dict) and "rank_tier" in profile:
            tier = profile["rank_tier"]
            if tier is None or type(tier) is int and 0 <= tier <= 85:
                result.rank_checked = True
                result.rank_tier = tier or None
        if isinstance(totals, list):
            for item in totals:
                if isinstance(item, dict) and item.get("field") == "duration":
                    seconds = item.get("sum")
                    matches = item.get("n")
                    if (
                        type(seconds) is int
                        and 0 <= seconds < 2**32
                        and type(matches) is int
                        and 0 <= matches < 2**32
                        and (matches > 0 or seconds == 0)
                    ):
                        estimated_seconds = seconds + matches * MATCHMAKING_MINUTES * 60
                        result.match_minutes = int(estimated_seconds * PLAYTIME_MULTIPLIER // 60)
                    break
        return result
