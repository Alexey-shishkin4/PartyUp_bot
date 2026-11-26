from datetime import datetime

import httpx
import pytest

from app.domain.deadlock import DeadlockStats, rank_name
from app.domain.steam import STEAM_INDIVIDUAL_BASE
from app.integrations.deadlock import DeadlockClient


ACCOUNT_ID = 123456789
STEAM_ID = str(STEAM_INDIVIDUAL_BASE + ACCOUNT_ID)


@pytest.mark.parametrize(
    ("second_duration", "abandoned_at", "seconds"),
    [(2700, None, 4500), (2700, 1200, 3000), (2700, 3000, 4500), (None, None, None)],
)
async def test_deadlock_sums_unique_match_durations_or_returns_count(second_duration, abandoned_at, seconds):
    endpoints = []

    def respond(request):
        assert request.url.host == "api.deadlock-api.com"
        assert request.headers["X-API-Key"] == "optional-test-key"
        assert not request.url.params
        endpoints.append(request.url.path)
        if request.url.path.endswith("/rank"):
            return httpx.Response(200, json={"badge": 116, "last_match": {"start_time": 1795614212}})
        return httpx.Response(200, json=[
            {"account_id": ACCOUNT_ID, "match_id": 1, "match_duration_s": 1800},
            {"account_id": ACCOUNT_ID, "match_id": 1, "match_duration_s": 1800},
            {"account_id": ACCOUNT_ID, "match_id": 2, "match_duration_s": second_duration},
            {
                "account_id": ACCOUNT_ID, "match_id": 2,
                "match_duration_s": second_duration, "abandoned_time_s": abandoned_at,
            },
            {"account_id": ACCOUNT_ID + 1, "match_id": 3, "match_duration_s": 600},
        ])

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        stats = await DeadlockClient(http, "optional-test-key").fetch(STEAM_ID)

    assert endpoints == [f"/v1/players/{ACCOUNT_ID}/rank", f"/v1/players/{ACCOUNT_ID}/match-history"]
    assert stats == DeadlockStats(116, datetime(2026, 11, 25, 13, 43, 32), 2, seconds)
    assert rank_name(stats.rank_badge) == "Eternus 6"


async def test_deadlock_keeps_rank_when_history_is_unavailable():
    def respond(request):
        if request.url.path.endswith("/rank"):
            return httpx.Response(200, json={"badge": 81, "last_match": None})
        return httpx.Response(503, headers={"Retry-After": "30"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        stats = await DeadlockClient(http).fetch(STEAM_ID)

    assert stats == DeadlockStats(81, None)
    assert rank_name(stats.rank_badge) == "Oracle 1"
