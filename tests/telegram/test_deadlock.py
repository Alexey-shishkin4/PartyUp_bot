import pytest

from app.domain.deadlock import DeadlockStats
from app.domain.steam import PublicProfile
from tests.telegram.harness import Harness


STEAM_ID = "76561198000000000"


@pytest.mark.parametrize("steam_minutes", [None, 125])
async def test_deadlock_card_displays_rank_and_match_time_stays_in_database(service, users, steam_minutes):
    user = users(1001)
    calls = []

    async def fetch(steam_id):
        calls.append(steam_id)
        return DeadlockStats(93, None, 5, 6060)

    games = []
    if steam_minutes is not None:
        games.append({"appid": 1422450, "name": "Deadlock", "playtime_forever": steam_minutes})

    class Steam:
        async def fetch(self, url):
            return PublicProfile(STEAM_ID, url, games, "library")

    harness = Harness(service, public_steam=Steam(), deadlock=fetch)
    await harness.update(1001, "/games")
    assert "accounts:choose:deadlock" not in str(harness.sent()[-1].reply_markup)
    await harness.update(1001, "/steam")
    await harness.update(1001, f"https://steamcommunity.com/profiles/{STEAM_ID}")
    if steam_minutes is None:
        assert not calls
        await harness.update(1001, callback="accounts:choose:deadlock")
    await harness.update(1001, "/profile")
    card = harness.sent()[-1].text
    assert "Deadlock (Phantom 3)" in card
    assert all(value not in card for value in ("1 ч 41 мин", "2 ч 5 мин", "матчей"))

    await harness.update(1001, callback="accounts:refresh:deadlock")
    assert calls == [STEAM_ID]
    row = service.deadlock.get(user.id, STEAM_ID)
    assert (row.details["rank_badge"], row.matches_count, row.details["match_seconds"]) == (93, 5, 6060)
    await harness.update(1001, callback="steam:disconnect")
    assert service.deadlock.get(user.id, STEAM_ID) is None
