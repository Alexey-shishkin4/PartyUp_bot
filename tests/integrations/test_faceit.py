import httpx
import pytest

from app.domain.errors import DomainError
from app.domain.faceit import FaceitLookup, FaceitStats
from app.integrations.faceit import FaceitClient


PLAYER_ID = "1efd000a-30e6-408b-8aaa-dd8219860b4b"
STEAM_ID = "76561198000000000"
PLAYER = {
    "player_id": PLAYER_ID,
    "nickname": "PartyUp-Test",
    "games": {
        "cs2": {"game_player_id": STEAM_ID, "faceit_elo": 1891, "skill_level": 9},
        "csgo": {"faceit_elo": 2200, "skill_level": 10},
    },
}


@pytest.mark.parametrize(
    ("lookup", "first_path", "params"),
    [
        (
            FaceitLookup.parse("https://www.faceit.com/ru/players/partyup-test"),
            "/data/v4/players",
            {"nickname": "partyup-test"},
        ),
        (FaceitLookup.parse(STEAM_ID), "/data/v4/players", {"game": "cs2", "game_player_id": STEAM_ID}),
        (FaceitLookup("player_id", PLAYER_ID), f"/data/v4/players/{PLAYER_ID}", {}),
    ],
)
async def test_faceit_lookup_uses_cs2_rating_and_lifetime_stats(lookup, first_path, params):
    requests = []

    def respond(request):
        assert request.url.host == "open.faceit.com"
        assert request.headers["Authorization"] == "Bearer faceit-test-key"
        requests.append((request.url.path, dict(request.url.params)))
        if request.url.path == first_path:
            return httpx.Response(200, json=PLAYER)
        return httpx.Response(200, json={
            "player_id": PLAYER_ID, "game_id": "cs2",
            "lifetime": {"Matches": "1200", "Wins": "654", "Win Rate %": "54.5"},
        })

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        stats = await FaceitClient(http, "faceit-test-key").fetch(lookup)
    assert requests == [(first_path, params), (f"/data/v4/players/{PLAYER_ID}/stats/cs2", {})]
    assert stats == FaceitStats(PLAYER_ID, "PartyUp-Test", 9, 1891, 1200, 654, 54.5)


async def test_faceit_keeps_rating_when_stats_are_missing():
    def respond(request):
        if request.url.path == "/data/v4/players":
            return httpx.Response(200, json=PLAYER)
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        stats = await FaceitClient(http, "key").fetch(FaceitLookup.parse("PartyUp-Test"))
    assert stats == FaceitStats(PLAYER_ID, "PartyUp-Test", 9, 1891, None, None, None)


async def test_faceit_does_not_use_csgo_rating_for_an_account_without_cs2():
    def respond(request):
        assert request.url.path == "/data/v4/players"
        return httpx.Response(200, json={**PLAYER, "games": {"csgo": PLAYER["games"]["csgo"]}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        with pytest.raises(DomainError, match="нет подключённой игры Counter-Strike 2"):
            await FaceitClient(http, "key").fetch(FaceitLookup.parse("PartyUp-Test"))


async def test_faceit_stops_requests_after_rate_limit():
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(429, headers={"Retry-After": "30"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        client = FaceitClient(http, "key")
        for _ in range(2):
            with pytest.raises(DomainError, match="FACEIT ограничил запросы"):
                await client.fetch(FaceitLookup.parse("PartyUp-Test"))
    assert len(requests) == 1
