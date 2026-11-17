import httpx
import pytest

from app.config import Settings
from app.service import DomainError
from app.steam_public import PublicSteamParser, normalize_profile_url, parse_games_html, parse_games_xml, xml_root

STEAM_ID = "76561198000000001"
PROFILE = f"<?xml version='1.0'?><profile><steamID64>{STEAM_ID}</steamID64><privacyState>public</privacyState></profile>"
RECENT_PROFILE = PROFILE.replace("</profile>", "<mostPlayedGames><mostPlayedGame>"
    "<gameName>Terraria</gameName><gameLink>https://store.steampowered.com/app/105600/</gameLink>"
    "<hoursPlayed>1.5</hoursPlayed></mostPlayedGame></mostPlayedGames></profile>")
GAMES = "<?xml version='1.0'?><gamesList><games><game><appID>105600</appID><name>Terraria</name>" \
        "<hoursOnRecord>1,234.5</hoursOnRecord><hoursLast2Weeks>2.5</hoursLast2Weeks></game></games></gamesList>"


@pytest.mark.parametrize("url", [
    "http://127.0.0.1/", "https://steamcommunity.com.evil.example/id/test",
    "https://steamcommunity.com@evil.example/id/test", "https://steamcommunity.com:8080/id/test",
    "https://steamcommunity.com/id/test/../../admin", "file:///etc/passwd", "https://steamcommunity.com/login",
])
def test_rejects_arbitrary_hosts_and_paths(url):
    with pytest.raises(DomainError):
        normalize_profile_url(url)


def test_normalizes_safe_profile_link_and_strips_query():
    assert normalize_profile_url("steamcommunity.com/id/test/?tracking=1#x") == "https://steamcommunity.com/id/test"
    assert normalize_profile_url("http://steamcommunity.com/profiles/" + STEAM_ID) == "https://steamcommunity.com/profiles/" + STEAM_ID


def test_xml_parser_handles_hours_and_empty_library():
    games = parse_games_xml(GAMES)
    assert games == [{"appid":105600,"name":"Terraria","playtime_forever":74070,"playtime_2weeks":150}]
    assert parse_games_xml("<gamesList><games/></gamesList>") == []


def test_legacy_html_parser_does_not_execute_scripts():
    text = '<script>var rgGames = [{"appid":105600,"name":"Terraria [test]","hours_forever":"3.5","hours":"1"}]; throw Error("never execute");</script>'
    assert parse_games_html(text)[0] == {"appid":105600,"name":"Terraria [test]","playtime_forever":210,"playtime_2weeks":60}
    assert parse_games_html("<html>Login required</html>") is None


def test_xml_entities_and_doctypes_are_rejected():
    with pytest.raises(DomainError):
        xml_root('<!DOCTYPE profile [<!ENTITY x "danger">]><profile>&x;</profile>')


async def test_fetches_public_library_without_key_cookies_or_external_hosts():
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(200,text=GAMES if "/games/" in request.url.path else PROFILE)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await PublicSteamParser(client,interval=0).fetch("https://steamcommunity.com/id/test")
    assert result.coverage == "library" and len(result.games) == 1
    assert len(calls) == 2
    assert all("key" not in request.url.params and "cookie" not in request.headers for request in calls)


async def test_login_redirect_respected_and_recent_data_marked_partial():
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(302,headers={"location":"https://steamcommunity.com/login/"}) if "/games/" in request.url.path else httpx.Response(200,text=RECENT_PROFILE)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await PublicSteamParser(client,interval=0).fetch("https://steamcommunity.com/id/test")
    assert result.coverage == "recent" and result.games[0]["playtime_2weeks"] == 90
    assert "Полная библиотека" in result.warning
    assert not any("/login" in request.url.path for request in calls)


async def test_offsite_redirect_is_never_followed():
    calls = []
    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(302,headers={"location":"http://127.0.0.1/private"}) if "/games/" in request.url.path else httpx.Response(200,text=PROFILE)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await PublicSteamParser(client,interval=0).fetch("https://steamcommunity.com/id/test")
    assert result.coverage == "profile_only"
    assert all(url.startswith("https://steamcommunity.com/") for url in calls)


async def test_private_profile_does_not_trigger_library_request():
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(200,text=PROFILE.replace("public", "private"))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await PublicSteamParser(client,interval=0).fetch("https://steamcommunity.com/id/test")
    assert result.coverage == "profile_only" and result.games == [] and len(calls) == 1


def test_live_link_mode_does_not_require_public_https():
    settings = Settings(_env_file=None,bot_token="123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijk")
    assert not settings.demo and settings.steam_mode == "link"


def test_unverified_links_do_not_block_real_owner(service, users):
    a, b = users(1), users(2)
    service.link_public_profile(a.id, STEAM_ID)
    service.link_public_profile(b.id, STEAM_ID)
    ticket = service.login_ticket(b.id)
    browser = service.begin_login(ticket)
    service.link_steam(ticket,browser,STEAM_ID)
    assert service.user(b.id).steam_verified
    assert not service.user(a.id).steam_verified


def test_partial_import_preserves_library_and_total_hours(service, users):
    a = users(1)
    service.import_games(a.id,[{"appid":105600,"name":"Terraria","playtime_forever":1000},
                               {"appid":730,"name":"CS2","playtime_forever":500}])
    service.import_games(a.id,[{"appid":105600,"name":"Terraria","playtime_2weeks":60}],complete=False)
    games = {game.app_id:game for game in service.user(a.id).games}
    assert games[105600].minutes == 1000 and games[105600].recent_minutes == 60
    assert games[730].minutes == 500
