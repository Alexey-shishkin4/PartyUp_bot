import asyncio
from collections import deque
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from time import monotonic, time
from urllib.parse import quote

import httpx

from app.domain.errors import DomainError
from app.domain.fortnite import RECENT_WINDOW, FortniteSnapshot, Playtime
from app.integrations.fortnite_stats import (
    account,
    dashboard_stats,
    mapping,
    osirion_account,
    osirion_playtime,
    osirion_ranks,
    raw_activity,
)

from .http import api_issue, api_skipped, read_json, request_api

OSIRION = "https://fnapi.osirion.gg"
API_FORTNITE = "https://prod.api-fortnite.com"
FORTNITE_API = "https://fortnite-api.com"
PROVIDERS = {OSIRION: "osirion", API_FORTNITE: "api-fortnite.com", FORTNITE_API: "fortnite-api.com"}


class RequestBudget:
    def __init__(self, *, per_minute=None, per_day=None):
        self.per_minute = per_minute
        self.per_day = per_day
        self.recent_requests = deque()
        self.day = None
        self.daily_requests = 0
        self.blocked_until = 0.0

    def reserve(self):
        now = monotonic()
        today = datetime.now(timezone.utc).date()
        if today != self.day:
            self.day, self.daily_requests = today, 0
        if now < self.blocked_until or (self.per_day is not None and self.daily_requests >= self.per_day):
            return False
        if self.per_minute is not None:
            while self.recent_requests and self.recent_requests[0] <= now - 60:
                self.recent_requests.popleft()
            if len(self.recent_requests) >= self.per_minute:
                return False
            self.recent_requests.append(now)
        self.daily_requests += 1
        return True

    def observe(self, response):
        remaining = response.headers.get("RateLimit-Remaining", response.headers.get("X-RateLimit-Remaining"))
        if response.status_code == 429 or remaining == "0":
            raw = response.headers.get(
                "Retry-After",
                response.headers.get("RateLimit-Reset", response.headers.get("X-RateLimit-Reset", "60")),
            )
            try:
                delay = int(raw)
                if delay > 1_000_000_000:
                    delay -= time()
            except ValueError:
                try:
                    delay = parsedate_to_datetime(raw).timestamp() - time()
                except (ValueError, TypeError, OverflowError):
                    delay = 60
            self.blocked_until = max(self.blocked_until, monotonic() + max(delay, 1))


class FortniteClient:
    def __init__(
        self, client: httpx.AsyncClient, *, api_fortnite_key="", fortnite_api_key="", daily_limit=10000
    ):
        self.client = client
        self.api_fortnite_key = api_fortnite_key
        self.fortnite_api_key = fortnite_api_key
        self.budgets = {
            OSIRION: RequestBudget(per_minute=50),
            API_FORTNITE: RequestBudget(per_day=daily_limit),
            FORTNITE_API: RequestBudget(per_minute=30),
        }

    async def _get(self, base, operation, path, *, params=None):
        provider = PROVIDERS[base]
        headers = {}
        if base == API_FORTNITE:
            if not self.api_fortnite_key:
                api_skipped(provider, operation, "missing_key")
                return {}
            headers = {"x-api-key": self.api_fortnite_key}
        elif base == FORTNITE_API:
            if not self.fortnite_api_key:
                api_skipped(provider, operation, "missing_key")
                return {}
            headers = {"Authorization": self.fortnite_api_key}
        budget = self.budgets[base]
        if not budget.reserve():
            api_skipped(provider, operation, "request_budget")
            return {}
        try:
            response = await request_api(
                self.client, provider, operation, "GET",
                base + path, params=params, headers=headers, timeout=10, follow_redirects=False
            )
            budget.observe(response)
            if (
                response.status_code == 403
                and base == API_FORTNITE
                and path.startswith("/api/v2/stats/")
                and "json" in response.headers.get("Content-Type", "").lower()
            ):
                raise DomainError(
                    "Fortnite закрыл доступ к статистике. Проверь настройку Public Game Stats в игре."
                )
            response.raise_for_status()
            payload = read_json(response, dict)
            if payload.get("success") is False:
                api_issue(provider, operation, "provider_rejected")
                return {}
            return payload
        except (httpx.HTTPError, httpx.InvalidURL, ValueError):
            return {}

    async def _resolve(self, nickname):
        result = osirion_account(
            await self._get(
                OSIRION,
                "account_lookup",
                "/v1/accounts/lookup-by-display-name",
                params={"displayName": nickname, "platform": "epic"},
            ),
            nickname,
        )
        if result:
            return result, None
        lookup = await self._get(
            API_FORTNITE, "account_lookup", "/api/v1/account/displayName/" + quote(nickname, safe="")
        )
        lookup = mapping(lookup.get("data")) or lookup
        result = account(lookup.get("id") or lookup.get("accountId"), lookup.get("displayName"), nickname)
        if result:
            return result, None
        return dashboard_stats(
            await self._get(
                FORTNITE_API, "account_lookup", "/v2/stats/br/v2",
                params={"name": nickname, "timeWindow": "lifetime"},
            ),
            nickname,
        )

    async def fetch(self, nickname, *, account_id=None):
        resolved, dashboard = (
            (account(account_id, nickname), None) if account_id else await self._resolve(nickname)
        )
        if not resolved:
            raise DomainError(
                "Профиль Epic не найден или сервис временно недоступен. Проверь ник и попробуй позже."
            )
        now = datetime.now(timezone.utc)
        stats_path = f"/api/v2/stats/{resolved.account_id}"
        stats_params = {"accountId": resolved.account_id}
        osirion, all_time, recent, ranked = await asyncio.gather(
            self._get(OSIRION, "lifetime_stats", "/v1/stats/account", params={**stats_params, "timeframe": "lifetime"}),
            self._get(API_FORTNITE, "lifetime_stats", stats_path),
            self._get(
                API_FORTNITE,
                "recent_stats",
                stats_path,
                params={"startTime": int((now - RECENT_WINDOW).timestamp()), "endTime": int(now.timestamp())},
            ),
            self._get(OSIRION, "ranked_stats", "/v1/ranked/account-ranks", params={**stats_params, "lang": "en"}),
        )
        playtime = osirion_playtime(osirion)
        if playtime is None and dashboard is None:
            _, dashboard = dashboard_stats(
                await self._get(
                    FORTNITE_API, "lifetime_stats", "/v2/stats/br/v2/" + resolved.account_id,
                    params={"timeWindow": "lifetime"},
                )
            )
        aggregate = playtime or dashboard
        raw = raw_activity(all_time)
        by_mode = dict(aggregate.modes) if aggregate else {}
        if raw:
            by_mode.update(raw.minutes)
        total = aggregate.total_minutes if aggregate else None
        source = aggregate.source if aggregate else "api-fortnite.com"
        if total is None and raw and raw.minutes:
            total, source = sum(raw.minutes.values()), "api-fortnite.com"
        return FortniteSnapshot(
            resolved,
            now.replace(tzinfo=None),
            Playtime(total, by_mode, source) if total is not None or by_mode else None,
            raw_activity(recent),
            osirion_ranks(ranked),
        )
