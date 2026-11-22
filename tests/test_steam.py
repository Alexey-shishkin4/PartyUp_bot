from datetime import datetime, timezone

import httpx
import pytest

from app.service import DomainError
from app.steam import NAMESPACE, OPENID, SteamClient


def assertion():
    claimed = "https://steamcommunity.com/openid/id/76561198000000001"
    return {"openid.mode":"id_res", "openid.ns":NAMESPACE,"openid.op_endpoint":OPENID,
            "openid.return_to":"https://party.example/steam/callback?ticket=abc",
            "openid.claimed_id":claimed,"openid.identity":claimed,
            "openid.response_nonce":datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") + "unique",
            "openid.signed":"op_endpoint,claimed_id,identity,return_to,response_nonce", "openid.sig":"signature"}


async def test_openid_verified_server_to_server():
    seen = []
    def handler(request):
        seen.append(request)
        return httpx.Response(200, text="ns:" + NAMESPACE + "\nis_valid:true\n")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        steam = SteamClient("secret", client)
        params = assertion()
        assert await steam.verify(params, params["openid.return_to"]) == "76561198000000001"
    assert seen[0].url == OPENID
    assert b"check_authentication" in seen[0].content


@pytest.mark.parametrize("field,value", [
    ("openid.return_to","https://evil.example/"), ("openid.op_endpoint","https://evil.example/"),
    ("openid.identity","https://steamcommunity.com/openid/id/76561198000000002"),
    ("openid.response_nonce","2000-01-01T00:00:00Zold"), ("openid.signed","identity"),
])
async def test_forged_assertions_rejected_before_network(field, value):
    def handler(request):
        pytest.fail("Invalid assertions must not trigger network requests")
    params = assertion()
    expected = params["openid.return_to"]
    params[field] = value
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(DomainError):
            await SteamClient("secret", client).verify(params, expected)


async def test_steam_rejection_respected():
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, text="is_valid:false"))) as client:
        params = assertion()
        with pytest.raises(DomainError):
            await SteamClient("key", client).verify(params, params["openid.return_to"])


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
    from app.steam_public import PublicProfile

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
