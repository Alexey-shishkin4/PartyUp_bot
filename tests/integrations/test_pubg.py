import httpx

from app.domain.pubg import MODES, PubgPlaytime, PubgRanking
from app.integrations.pubg import PubgClient


ACCOUNT = "account." + "a" * 32
SEASON = "division.bro.official.pc-2018-44"
PLAYER = {"id": ACCOUNT, "attributes": {"name": "Example"}}
LIFETIME = {"data": {
    "attributes": {"gameModeStats": {mode: {"timeSurvived": n * 3600.5} for n, mode in enumerate(MODES)}},
    "relationships": {"player": {"data": {"id": ACCOUNT}}},
}}
RANKS = {
    "squad-fpp": {"currentTier": {"tier": "Gold", "subTier": "3"}, "currentRankPoint": 1785,
                  "bestTier": {"tier": "Diamond", "subTier": "1"}},
}


async def test_pubg_uses_public_lifetime_and_one_official_call_for_a_warm_rank_refresh():
    calls = []

    def respond(request):
        host, path = request.url.host, request.url.path
        calls.append((host, path))
        if host == "pubg-api.org":
            assert "Authorization" not in request.headers
            if path == "/api/players":
                return httpx.Response(200, json=PLAYER)
            assert path == "/api/lifetime/stats"
            assert request.url.params["accountId"] == ACCOUNT
            return httpx.Response(200, json=LIFETIME)
        assert host == "api.pubg.com"
        assert request.headers["Authorization"] == "Bearer pubg-jwt"
        if path == "/shards/steam/seasons":
            return httpx.Response(200, json={"data": [{"id": SEASON, "attributes": {"isCurrentSeason": True}}]})
        assert path == f"/shards/steam/players/{ACCOUNT}/seasons/{SEASON}/ranked"
        return httpx.Response(200, json={"data": {"attributes": {"rankedGameModeStats": RANKS}}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        client = PubgClient(http, "pubg-jwt")
        first = await client.fetch("steam", "example")
        assert first.account.account_id == ACCOUNT
        assert first.playtime == PubgPlaytime({mode: int(i * 3600.5) for i, mode in enumerate(MODES)}, "public")
        assert first.ranking == PubgRanking({"squad-fpp": "Gold 3 · 1785 RP"}, "official", SEASON)
        assert len(calls) == 4
        calls.clear()
        refreshed = await client.fetch("steam", "Example", account_id=ACCOUNT, need_playtime=False)
        assert refreshed.playtime is None
        assert refreshed.ranking == first.ranking
        assert calls == [("api.pubg.com", f"/shards/steam/players/{ACCOUNT}/seasons/{SEASON}/ranked")]


async def test_pubg_respects_official_budget_and_labels_public_rank_fallback():
    calls = []

    def respond(request):
        calls.append(request.url.host)
        if request.url.host == "api.pubg.com":
            assert request.url.path == "/shards/steam/seasons"
            return httpx.Response(200, json={"data": [{"id": SEASON, "attributes": {"isCurrentSeason": True}}]})
        assert request.url.path == "/api/stats/rank"
        assert "Authorization" not in request.headers
        return httpx.Response(200, json=RANKS)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        client = PubgClient(http, "pubg-jwt", requests_per_minute=1)
        stats = await client.fetch("steam", "Example", account_id=ACCOUNT, need_playtime=False)
    assert stats.ranking == PubgRanking({"squad-fpp": "Gold 3 · 1785 RP"}, "public")
    assert calls == ["api.pubg.com", "pubg-api.org"]


async def test_pubg_keeps_lifetime_stats_when_both_rank_sources_are_unavailable():
    def respond(request):
        if request.url.path == "/api/lifetime/stats":
            return httpx.Response(200, json=LIFETIME)
        return httpx.Response(429, headers={"Retry-After": "30"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        stats = await PubgClient(http, "pubg-jwt").fetch("steam", "Example", account_id=ACCOUNT)
    assert stats.playtime is not None
    assert stats.ranking is None
