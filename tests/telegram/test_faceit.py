from datetime import timedelta

import httpx
import pytest

from app.domain.errors import DomainError
from app.domain.faceit import FaceitLookup, FaceitStats
from app.integrations.faceit import FaceitClient
from app.integrations.steam import SteamClient
from app.persistence.models import FaceitProfile, User, utcnow
from tests.telegram.harness import Harness


PLAYER_ID = "1efd000a-30e6-408b-8aaa-dd8219860b4b"
STEAM_ID = "76561198000000000"


async def test_faceit_links_steam_reads_cached_stats_and_keeps_them_on_refresh_failure(service, users, monkeypatch):
    user = users(1001)
    with service.db.session.begin() as session:
        session.get(User, user.id).steam_id = STEAM_ID
    lookups = []
    now = 100.0
    monkeypatch.setattr("app.telegram.routers.game_accounts.time", lambda: now)

    async def fetch(lookup):
        lookups.append(lookup)
        if lookup.kind == "player_id":
            raise DomainError("Статистика FACEIT временно недоступна.")
        if lookup.kind == "steam_id":
            return FaceitStats(PLAYER_ID, "PartyUp-Test", 9, 1891, 1200, 654, 54.5, steam_id=STEAM_ID, source="wok")
        return FaceitStats(PLAYER_ID, "PartyUp-Test", 9, 1891, 1200, 654, 54.5)

    harness = Harness(service, faceit=fetch)
    await harness.update(1001, "/games")
    await harness.update(1001, callback="accounts:choose:faceit")
    await harness.update(1001, STEAM_ID)
    await harness.update(1001, "/profile")
    text = harness.sent()[-1].text
    assert "Counter-Strike 2 (FACEIT 9)" in text
    assert "1891" not in text and "PartyUp-Test" not in text
    assert service.faceit.get(user.id).source == "wok"

    await harness.update(1001, callback="accounts:refresh:faceit")
    assert lookups == [FaceitLookup("steam_id", STEAM_ID)]

    with service.db.session.begin() as session:
        session.get(FaceitProfile, user.id).checked_at = utcnow() - timedelta(minutes=16)
    now += 20
    await harness.update(1001, callback="accounts:refresh:faceit")
    assert lookups[-1] == FaceitLookup("player_id", PLAYER_ID, steam_id=STEAM_ID)
    assert "временно недоступна" in harness.sent()[-1].text
    assert service.faceit.get(user.id).elo == 1891

    await harness.update(1001, callback="accounts:disconnect:faceit")
    assert service.faceit.get(user.id) is None

    now += 20
    await harness.update(1001, callback="accounts:choose:faceit")
    await harness.update(1001, "PartyUp-Test")
    assert lookups[-1] == FaceitLookup("nickname", "PartyUp-Test")
    assert service.faceit.get(user.id)
    await harness.update(1001, "/delete")
    await harness.update(1001, callback="delete:yes")
    assert service.faceit.get(user.id) is None


@pytest.mark.parametrize(
    ("steam_path", "minutes"),
    [(f"profiles/{STEAM_ID}", 60), ("id/partyup-test", 60), ("id/partyup-test", 0)],
)
async def test_faceit_automatically_reuses_steam_id_from_link(service, users, steam_path, minutes):
    user = users(1001)
    requests = []

    def respond(request):
        host, path = request.url.host, request.url.path
        requests.append((host, path))
        if host == "api.steampowered.com":
            if "/ResolveVanityURL/" in path:
                assert request.url.params["vanityurl"] == "partyup-test"
                return httpx.Response(200, json={"response": {"success": 1, "steamid": STEAM_ID}})
            assert request.url.params["steamid"] == STEAM_ID
            return httpx.Response(200, json={"response": {"game_count": 1, "games": [
                {"appid": 730, "name": "Counter-Strike 2", "playtime_forever": minutes},
            ]}})
        assert host == "woksteamapi.com"
        if path == "/faceit/data/v4/players":
            assert dict(request.url.params) == {"game": "cs2", "game_player_id": STEAM_ID}
            return httpx.Response(200, json={
                "player_id": PLAYER_ID, "nickname": "PartyUp-Test",
                "games": {"cs2": {"skill_level": 9, "faceit_elo": 1891}},
            })
        assert path == f"/faceit/data/v4/players/{PLAYER_ID}/stats/cs2"
        return httpx.Response(200, json={"lifetime": {"Matches": "1200"}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        faceit = FaceitClient(http, "", wok_api_key="wok-test-key")
        harness = Harness(service, steam=SteamClient("steam-test-key", http), faceit=faceit.fetch)
        await harness.update(1001, "/steam")
        await harness.update(1001, f"https://steamcommunity.com/{steam_path}")
        assert service.profiles.user(user.id).steam_id == STEAM_ID
        calls = [(host, path) for host, path in requests if host == "woksteamapi.com"]
        if minutes == 0:
            assert service.faceit.get(user.id) is None
            assert not calls
            return
        row = service.faceit.get(user.id)
        assert row.steam_id == STEAM_ID and row.match_count == 1200
        assert calls == [
            ("woksteamapi.com", "/faceit/data/v4/players"),
            ("woksteamapi.com", f"/faceit/data/v4/players/{PLAYER_ID}/stats/cs2"),
        ]
