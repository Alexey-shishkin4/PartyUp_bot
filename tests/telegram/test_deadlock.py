from datetime import timedelta

import pytest

from app.domain.deadlock import DeadlockStats
from app.domain.steam import PublicProfile
from app.persistence.models import DeadlockProfile, utcnow
from tests.telegram.harness import Harness


STEAM_ID = "76561198000000000"


@pytest.mark.parametrize("steam_minutes", [None, 125])
async def test_deadlock_displays_match_time_even_when_steam_hours_exist(service, users, steam_minutes):
    user = users(1001)
    calls = []

    async def fetch(steam_id):
        calls.append(steam_id)
        return DeadlockStats(93, None, 5, 6060)

    harness = Harness(service, deadlock=fetch)
    await harness.update(1001, "/deadlock")
    assert "через /steam" in harness.sent()[-1].text
    assert not calls

    games = []
    if steam_minutes is not None:
        games.append({"appid": 1422450, "name": "Deadlock", "playtime_forever": steam_minutes})
    service.steam.save_public_steam(
        user.id, PublicProfile(STEAM_ID, f"https://steamcommunity.com/profiles/{STEAM_ID}", games, "library")
    )
    if steam_minutes is not None:
        service.deadlock.save(user.id, STEAM_ID, DeadlockStats(93, None), expected_checked_at=None)
        with service.db.session.begin() as session:
            session.get(DeadlockProfile, user.id).checked_at = utcnow() - timedelta(minutes=2)
    await harness.update(1001, "/deadlock")
    card = harness.sent()[-1].text
    assert "Phantom 3" in card
    assert "Время в матчах по доступной истории: ≈ 1 ч 41 мин" in card
    assert "Матчи в доступной истории: 5" in card
    assert "2 ч 5 мин" not in card

    await harness.update(1001, callback="deadlock:refresh")
    assert calls == [STEAM_ID]
    assert service.deadlock.get(user.id, STEAM_ID).rank_badge == 93
    await harness.update(1001, callback="steam:disconnect")
    assert service.deadlock.get(user.id, STEAM_ID) is None
