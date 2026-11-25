import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from app.domain.errors import DomainError
from app.domain.riot import (
    LEAGUE_FLEX,
    LEAGUE_SOLO,
    VALORANT_RANKED_ACTS,
    VALORANT_RIOT_HISTORY,
    RiotGame,
    RiotId,
    RiotStats,
)
from app.integrations.riot import RiotClient

ACCOUNT = RiotId("Jett Main", "EUW")


@pytest.mark.asyncio
async def test_ugi_uses_total_across_unfiltered_history_and_does_not_call_fallback():
    requests = []

    def respond(request):
        assert request.url.host == "api.ugilabs.com"
        assert request.headers["Authorization"] == "Bearer ugi-key"
        requests.append(request.url.raw_path)
        if request.url.path.startswith("/v1/matches/"):
            return httpx.Response(
                200,
                json={
                    "data": {
                        "name": "Jett Main",
                        "tag": "EUW",
                        "mode": None,
                        "total_matches": 1,
                        "pagination": {"size": 1, "returned": 1, "total": 391},
                    },
                },
            )
        return httpx.Response(
            200,
            json={
                "status": 200,
                "data": {
                    "name": "Jett Main",
                    "tag": "EUW",
                    "current_data": {"currenttierpatched": "Gold 2", "games": 15},
                    "by_season": [{"season_id": "act-old", "games": 61}],
                },
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        stats = await RiotClient(http, ugi_key="ugi-key", henrik_key="backup").fetch(
            RiotGame.VALORANT, "EU", ACCOUNT
        )
    assert stats == RiotStats(ACCOUNT, "Gold 2", 391, VALORANT_RIOT_HISTORY, "ugi", "eu")
    assert requests == [b"/v1/matches/eu/Jett%20Main/EUW?size=1", b"/v1/rank/eu/Jett%20Main/EUW"]


@pytest.mark.asyncio
async def test_henrik_sums_distinct_acts_in_one_request_when_ugi_is_rate_limited():
    requests = []

    def respond(request):
        path = request.url.path
        requests.append(request.url.host)
        if request.url.host == "api.ugilabs.com":
            return httpx.Response(429, headers={"Retry-After": "120"})
        assert request.url.host == "api.henrikdev.xyz"
        assert request.headers["Authorization"] == "henrik-key"
        assert path == "/valorant/v3/mmr/eu/pc/Jett Main/EUW"
        return httpx.Response(
            200,
            json={
                "status": 200,
                "data": {
                    "account": {"name": "Jett Main", "tag": "EUW"},
                    "current": {"tier": {"name": "Gold 2"}},
                    "seasonal": [
                        {"season": {"id": "act-previous"}, "games": 83},
                        {"season": {"id": "act-current"}, "games": 17},
                        {"season": {"id": "ACT-PREVIOUS"}, "games": 82},
                        {"season": {"id": "act-malformed"}, "games": "123"},
                    ],
                },
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        client = RiotClient(http, ugi_key="ugi-key", henrik_key="henrik-key")
        for _ in range(2):
            stats = await client.fetch(RiotGame.VALORANT, "eu", ACCOUNT)
            assert stats == RiotStats(ACCOUNT, "Gold 2", 100, VALORANT_RANKED_ACTS, "henrik", "eu")
    assert requests == ["api.ugilabs.com", "api.henrikdev.xyz", "api.henrikdev.xyz"]


@pytest.mark.asyncio
async def test_short_riot_history_does_not_replace_a_larger_ranked_total():
    def respond(request):
        response = {"name": "Jett Main", "tag": "EUW"}
        if request.url.path.startswith("/v1/matches/"):
            response["pagination"] = {"total": 100}
        else:
            response.update(
                current_data={"currenttierpatched": "Gold 2", "games": 20},
                by_season=[{"season_id": "current", "games": 20}, {"season_id": "past", "games": 141}],
            )
        return httpx.Response(200, json={"data": response})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        stats = await RiotClient(http, ugi_key="ugi-key").fetch(RiotGame.VALORANT, "eu", ACCOUNT)
    assert stats == RiotStats(ACCOUNT, "Gold 2", 161, VALORANT_RANKED_ACTS, "ugi", "eu")


@pytest.mark.asyncio
async def test_ugi_count_can_be_shown_with_henrik_rank():
    def respond(request):
        if request.url.path.startswith("/v1/rank/"):
            return httpx.Response(503)
        if request.url.host == "api.ugilabs.com":
            return httpx.Response(
                200, json={"data": {"name": "Jett Main", "tag": "euw", "pagination": {"total": 140}}}
            )
        return httpx.Response(
            200,
            json={
                "data": {
                    "account": {"name": "Jett Main", "tag": "EUW"},
                    "current": {"tier": {"name": "Gold 2"}},
                    "seasonal": [{"season": {"id": "past-act"}, "games": 99}],
                },
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        stats = await RiotClient(http, ugi_key="ugi-key", henrik_key="henrik-key").fetch(
            RiotGame.VALORANT, "eu", ACCOUNT
        )
    assert stats == RiotStats(
        RiotId("Jett Main", "euw"), "Gold 2", 140, VALORANT_RIOT_HISTORY, "henrik_ugi", "eu"
    )


@pytest.mark.parametrize("henrik_status", [200, 503])
@pytest.mark.asyncio
async def test_valorant_detects_region_before_calling_stats(henrik_status):
    requests = []

    def respond(request):
        host, path = request.url.host, request.url.path
        requests.append(path)
        if host == "api.henrikdev.xyz":
            assert request.headers["Authorization"] == "henrik-key"
            return httpx.Response(henrik_status, json={"data": {"region": "na"}})
        if host == "europe.api.riotgames.com":
            assert request.headers["X-Riot-Token"] == "riot-key"
            if "/accounts/" in path:
                return httpx.Response(
                    200,
                    json={"gameName": "Jett Main", "tagLine": "EUW", "puuid": "riot-puuid"},
                )
            return httpx.Response(200, json={"activeShard": "na"})
        assert host == "api.ugilabs.com"
        account = {"name": "Jett Main", "tag": "EUW"}
        if path.startswith("/v1/matches/"):
            return httpx.Response(200, json={"data": account | {"pagination": {"total": 100}}})
        return httpx.Response(
            200, json={"data": account | {"current_data": {"currenttierpatched": "Gold 2"}}}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        stats = await RiotClient(http, ugi_key="ugi-key", henrik_key="henrik-key", riot_key="riot-key").fetch(
            RiotGame.VALORANT, None, ACCOUNT
        )
    expected = ["/valorant/v2/account/Jett Main/EUW"]
    if henrik_status != 200:
        expected.extend(
            [
                "/riot/account/v1/accounts/by-riot-id/Jett Main/EUW",
                "/riot/account/v1/active-shards/by-game/val/by-puuid/riot-puuid",
            ]
        )
    assert requests == expected + ["/v1/matches/na/Jett Main/EUW", "/v1/rank/na/Jett Main/EUW"]
    assert stats == RiotStats(ACCOUNT, "Gold 2", 100, VALORANT_RIOT_HISTORY, "ugi", "na")


@pytest.mark.asyncio
async def test_iodid_uses_exact_riot_id_and_ranked_queue_without_a_key():
    def respond(request):
        if request.url.host == "mcp-api.op.gg":
            return httpx.Response(503)
        assert request.url.host == "api.safjnest.com"
        assert request.url.path == "/api/lol/search"
        assert request.url.params["q"] == "Jett Main#EUW"
        assert "Authorization" not in request.headers and "X-Riot-Token" not in request.headers
        return httpx.Response(
            200,
            json=[
                {"summoner": {"riotId": "Jett Main#EUWOP", "region": "NA1"}},
                {
                    "summoner": {"riotId": "Jett Main#EUW", "region": "EUW1"},
                    "ranks": {
                        "RANKED_SOLO_5X5": {"tier": "EMERALD_II", "wins": 31, "losses": 24},
                        "RANKED_FLEX_SR": {"tier": "GOLD_I", "wins": 38, "losses": 37},
                    },
                },
            ],
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        stats = await RiotClient(http).fetch(RiotGame.LEAGUE, None, ACCOUNT)
    assert stats == RiotStats(ACCOUNT, "Emerald II", 55, LEAGUE_SOLO, "iodid", "euw1")


@pytest.mark.asyncio
async def test_iodid_reads_profile_when_search_has_no_ranked_entries():
    summoner = {"riotId": "Jett Main#EUW", "region": "EUW1", "puuid": "iodid-puuid"}

    def respond(request):
        if request.url.host == "mcp-api.op.gg":
            return httpx.Response(503)
        assert request.url.host == "api.safjnest.com"
        if request.url.path == "/api/lol/search":
            assert request.url.params["shard"] == "EUW1"
            return httpx.Response(200, json=[{"summoner": summoner, "ranks": {}}])
        assert request.url.path == "/api/lol/EUW1/profile/iodid-puuid"
        return httpx.Response(
            200,
            json={
                "summoner": summoner,
                "ranks": {"RANKED_FLEX_SR": {"tier": "MASTER_I", "wins": 9, "losses": 4}},
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        stats = await RiotClient(http).fetch(RiotGame.LEAGUE, "euw", ACCOUNT)
    assert stats == RiotStats(ACCOUNT, "Master", 13, LEAGUE_FLEX, "iodid", "euw1")


@pytest.mark.asyncio
async def test_pending_iodid_profile_is_reported_without_requiring_a_riot_key():
    def respond(request):
        if request.url.host == "mcp-api.op.gg":
            return httpx.Response(503)
        if request.url.path == "/api/lol/search":
            return httpx.Response(
                200,
                json=[{"summoner": {"riotId": "Jett Main#EUW", "region": "EUW1", "puuid": "iodid-puuid"}}],
            )
        assert request.url.path == "/api/lol/EUW1/profile/iodid-puuid"
        return httpx.Response(202, json={"code": "PENDING"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        with pytest.raises(DomainError, match="ещё загружает"):
            await RiotClient(http).fetch(RiotGame.LEAGUE, None, ACCOUNT)


@pytest.mark.parametrize(
    ("region", "platform", "queue"),
    [(None, "euw1", LEAGUE_SOLO), ("ph2", "sg2", LEAGUE_FLEX)],
)
@pytest.mark.asyncio
async def test_league_selects_platform_and_queue(region, platform, queue):
    requests = []
    puuid = "riot_account-puuid"

    def respond(request):
        requests.append((request.url.host, request.url.path))
        if request.url.host == "mcp-api.op.gg":
            assert "X-Riot-Token" not in request.headers
            return httpx.Response(503)
        if request.url.host == "api.safjnest.com":
            assert "X-Riot-Token" not in request.headers
            return httpx.Response(503)
        assert request.headers["X-Riot-Token"] == "riot-key"
        if request.url.host == "europe.api.riotgames.com":
            if "/accounts/" in request.url.path:
                return httpx.Response(200, json={"puuid": puuid, "gameName": "Jett Main", "tagLine": "EUW"})
            assert request.url.path == f"/riot/account/v1/region/by-game/lol/by-puuid/{puuid}"
            return httpx.Response(200, json={"region": "EUW1"})
        entries = [{"queueType": LEAGUE_FLEX, "tier": "SILVER", "rank": "III", "wins": 11, "losses": 9}]
        if queue == LEAGUE_SOLO:
            entries.append({"queueType": LEAGUE_SOLO, "tier": "GOLD", "rank": "II", "wins": 12, "losses": 8})
        return httpx.Response(200, json=entries)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        stats = await RiotClient(http, riot_key="riot-key").fetch(RiotGame.LEAGUE, region, ACCOUNT)
    expected = [
        ("api.safjnest.com", "/api/lol/search") if region is None else ("mcp-api.op.gg", "/mcp"),
        ("europe.api.riotgames.com", "/riot/account/v1/accounts/by-riot-id/Jett Main/EUW"),
    ]
    if region is None:
        expected.append(("europe.api.riotgames.com", f"/riot/account/v1/region/by-game/lol/by-puuid/{puuid}"))
    expected.append((f"{platform}.api.riotgames.com", f"/lol/league/v4/entries/by-puuid/{puuid}"))
    assert requests == expected
    assert stats == RiotStats(
        ACCOUNT, "Gold II" if queue == LEAGUE_SOLO else "Silver III", 20, queue, "riot", platform
    )


@pytest.mark.parametrize("summoner_status", [200, 404])
@pytest.mark.asyncio
async def test_empty_league_entries_verify_summoner_region(summoner_status):
    def respond(request):
        if request.url.host == "mcp-api.op.gg":
            return httpx.Response(404)
        if request.url.host == "api.safjnest.com":
            return httpx.Response(200, json=[])
        if request.url.host == "europe.api.riotgames.com":
            return httpx.Response(
                200, json={"gameName": "Jett Main", "tagLine": "EUW", "puuid": "riot-puuid"}
            )
        assert request.url.host == "kr.api.riotgames.com"
        if request.url.path.startswith("/lol/league/"):
            return httpx.Response(200, json=[])
        assert request.url.path == "/lol/summoner/v4/summoners/by-puuid/riot-puuid"
        return httpx.Response(summoner_status, json={"puuid": "riot-puuid"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        client = RiotClient(http, riot_key="riot-key")
        if summoner_status == 404:
            with pytest.raises(DomainError, match="не найден"):
                await client.fetch(RiotGame.LEAGUE, "kr", ACCOUNT)
        else:
            assert await client.fetch(RiotGame.LEAGUE, "kr", ACCOUNT) == RiotStats(
                ACCOUNT, None, None, LEAGUE_SOLO, "riot", "kr"
            )


@pytest.mark.parametrize(
    ("days_old", "riot_key", "expected_source", "expected_rank", "expected_matches"),
    [
        (0, "riot-key", "opgg", "Emerald IV", 683),
        (7, "", "opgg_cached", "Emerald IV", 683),
        (7, "riot-key", "riot", "Platinum I", 695),
    ],
)
@pytest.mark.asyncio
async def test_opgg_uses_source_date_and_prefers_riot_for_old_profile(
    days_old, riot_key, expected_source, expected_rank, expected_matches
):
    updated_at = (datetime.now(timezone.utc) - timedelta(days=days_old, minutes=5)).isoformat()
    phases = []

    def respond(request):
        if request.url.host == "api.safjnest.com":
            phases.append("region lookup")
            assert "shard" not in request.url.params
            return httpx.Response(200, json=[
                {
                    "summoner": {"riotId": "Jett Main#EUW", "region": "EUW1"},
                    "ranks": {"RANKED_SOLO_5X5": {"tier": "EMERALD_III", "wins": 323, "losses": 285}},
                },
            ])
        if request.url.host != "mcp-api.op.gg":
            assert expected_source == "riot"
            assert request.headers["X-Riot-Token"] == riot_key
            if "/accounts/by-riot-id/" in request.url.path:
                return httpx.Response(200, json={"puuid": "riot-puuid", "gameName": "Jett Main", "tagLine": "EUW"})
            if "/region/" in request.url.path:
                return httpx.Response(200, json={"region": "EUW1"})
            assert request.url.host == "euw1.api.riotgames.com"
            return httpx.Response(200, json=[
                {"queueType": LEAGUE_SOLO, "tier": "PLATINUM", "rank": "I", "wins": 361, "losses": 334},
            ])
        assert "X-Riot-Token" not in request.headers
        if request.method == "DELETE":
            phases.append("close")
            assert request.headers["Mcp-Session-Id"] == "session"
            return httpx.Response(204)
        data = json.loads(request.content)
        phases.append(data["method"])
        if data["method"] == "initialize":
            return httpx.Response(
                200,
                headers={"Mcp-Session-Id": "session"},
                json={"jsonrpc": "2.0", "id": data["id"], "result": {"protocolVersion": "2025-06-18"}},
            )
        assert request.headers["Mcp-Session-Id"] == "session"
        assert request.headers["MCP-Protocol-Version"] == "2025-06-18"
        if data["method"] == "notifications/initialized":
            return httpx.Response(202)
        assert data["params"]["name"] == "lol_get_summoner_profile"
        assert data["params"]["arguments"]["region"] == "EUW"
        profile = (
            "class Summoner: game_name,tagline,updated_at,league_stats\n\n"
            f'LolGetSummonerProfile(Data(Summoner("Jett Main","EUW","{updated_at}",['
            'LeagueStat("FLEXRANKED",TierInfo("SILVER",3,35),11,9,null),'
            'LeagueStat("SOLORANKED",TierInfo("EMERALD",4,55),359,324,null)])))'
        )
        rpc = {"jsonrpc": "2.0", "id": data["id"], "result": {"content": [{"type": "text", "text": profile}]}}
        return httpx.Response(200, headers={"Content-Type": "text/event-stream"}, text=f"data: {json.dumps(rpc)}\n\n")

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        stats = await RiotClient(http, riot_key=riot_key).fetch(RiotGame.LEAGUE, None, ACCOUNT)
    assert phases == ["region lookup", "initialize", "notifications/initialized", "tools/call", "close"]
    assert stats == RiotStats(ACCOUNT, expected_rank, expected_matches, LEAGUE_SOLO, expected_source, "euw1")
