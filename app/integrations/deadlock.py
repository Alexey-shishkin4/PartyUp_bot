import logging
from datetime import datetime, timezone
from time import monotonic

import httpx

from app.domain.deadlock import DeadlockStats, rank_name
from app.domain.errors import DomainError
from app.domain.steam import STEAM_INDIVIDUAL_BASE


BASE_URL = "https://api.deadlock-api.com/v1/players"
UNAVAILABLE = "Deadlock API временно недоступен. Попробуй позже."
log = logging.getLogger(__name__)


class DeadlockClient:
    def __init__(self, http: httpx.AsyncClient, api_key: str = ""):
        self.http = http
        api_key = api_key.strip()
        self.headers = {"X-API-Key": api_key} if api_key else {}
        self.cooldowns: dict[str, float] = {}

    async def fetch(self, steam_id: str) -> DeadlockStats:
        account_id = int(steam_id) - STEAM_INDIVIDUAL_BASE if steam_id.isascii() and steam_id.isdecimal() else 0
        if not 0 < account_id < 2**32:
            raise DomainError("Не удалось определить аккаунт Steam. Подключи профиль заново через /steam.")

        rank = await self._get(account_id, "rank")
        badge = rank.get("badge") if isinstance(rank, dict) else None
        if type(badge) is not int or badge != 0 and rank_name(badge) is None:
            raise DomainError("Deadlock API вернул неизвестный ранг. Попробуй позже.")
        last_match = rank.get("last_match")
        started = last_match.get("start_time") if isinstance(last_match, dict) else None
        ranked_at = (
            datetime.fromtimestamp(started, timezone.utc).replace(tzinfo=None)
            if badge and type(started) is int and 0 < started < 2**32 else None
        )
        try:
            history = await self._get(account_id, "match-history")
        except DomainError:
            return DeadlockStats(badge, ranked_at)
        if not isinstance(history, list):
            log.warning("Deadlock match-history returned an unexpected response")
            return DeadlockStats(badge, ranked_at)

        by_match: dict[int, int] = {}
        abandoned_at: dict[int, int] = {}
        for row in history:
            if not isinstance(row, dict) or row.get("account_id") != account_id:
                continue
            match_id = row.get("match_id")
            if type(match_id) is not int or match_id <= 0:
                continue
            duration = row.get("match_duration_s")
            seconds = duration if type(duration) is int and duration > 0 else 0
            by_match[match_id] = max(by_match.get(match_id, 0), seconds)
            abandon = row.get("abandoned_time_s")
            if type(abandon) is int and 0 < abandon <= seconds:
                abandoned_at[match_id] = min(abandon, abandoned_at.get(match_id, abandon))
        if history and not by_match:
            return DeadlockStats(badge, ranked_at)
        # Partially timed histories still provide a match count, without undercounting time.
        seconds = (
            sum(abandoned_at.get(match_id, duration) for match_id, duration in by_match.items())
            if by_match and all(by_match.values()) else None
        )
        return DeadlockStats(badge, ranked_at, len(by_match), seconds)

    async def _get(self, account_id: int, endpoint: str):
        if monotonic() < self.cooldowns.get(endpoint, 0):
            raise DomainError("Deadlock API попросил подождать перед следующим запросом.")
        try:
            response = await self.http.get(
                f"{BASE_URL}/{account_id}/{endpoint}", headers=self.headers,
                timeout=12, follow_redirects=False,
            )
            if response.status_code in {429, 503, 504}:
                header = response.headers.get("Retry-After", "")
                delay = int(header) if header.isascii() and header.isdecimal() and len(header) <= 9 else 60
                self.cooldowns[endpoint] = max(self.cooldowns.get(endpoint, 0), monotonic() + max(1, delay))
            if response.status_code == 429:
                raise DomainError("Deadlock API ограничил запросы. Попробуй позже.")
            response.raise_for_status()
            return response.json()
        except httpx.HTTPStatusError as error:
            log.warning("Deadlock %s returned HTTP %s", endpoint, error.response.status_code)
        except (httpx.RequestError, ValueError) as error:
            log.warning("Deadlock %s request failed (%s)", endpoint, type(error).__name__)
        raise DomainError(UNAVAILABLE)
