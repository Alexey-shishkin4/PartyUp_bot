import httpx
import pytest

from app.domain.errors import DomainError
from app.integrations.steam import SteamClient


async def test_private_library_and_free_games_parameter():
    seen = []
    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"response":{}})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(DomainError, match="скрыта"):
            await SteamClient("key", client).owned_games("76561198000000001")
    assert seen[0].url.params["include_played_free_games"] == "1"


async def test_empty_public_library_is_valid():
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"response":{"game_count":0}}))) as client:
        assert await SteamClient("key", client).owned_games("76561198000000001") == []


async def test_link_falls_back_to_recent_public_games_when_web_api_has_no_library():
    from app.domain.steam import PublicProfile

    class RecentPublicProfile:
        async def fetch(self, url):
            assert url == "https://steamcommunity.com/profiles/76561198000000001"
            return PublicProfile("76561198000000001", url, coverage="recent",
                                 games=[{"appid":570, "name":"Dota 2", "playtime_2weeks":240}])

    def handler(request):
        assert request.url.host == "api.steampowered.com"
        return httpx.Response(200, json={"response":{}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        profile = await SteamClient("key", client).fetch_profile(
            "https://steamcommunity.com/profiles/76561198000000001", RecentPublicProfile()
        )
    assert profile.coverage == "recent"
    assert profile.games[0]["playtime_2weeks"] == 240


@pytest.mark.parametrize("available", ["library", "recent"])
async def test_steam_profile_import_uses_the_available_web_api_response(available):
    endpoints = {
        "GetOwnedGames": ("library", "game_count", {"appid":730, "name":"Counter-Strike 2", "playtime_forever":300}),
        "GetRecentlyPlayedGames": ("recent", "total_count",
                                   {"appid":570, "name":"Dota 2", "playtime_forever":600, "playtime_2weeks":23}),
    }

    def handler(request):
        source, count, game = endpoints[request.url.path.split("/")[-3]]
        if source == available:
            return httpx.Response(200, json={"response":{count:1, "games":[game]}})
        return httpx.Response(503)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        profile = await SteamClient("key", client).game_profile("76561198000000001")
    assert profile.coverage == available
    assert [game["appid"] for game in profile.games] == [730 if available == "library" else 570]
