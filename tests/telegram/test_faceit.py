from datetime import timedelta

from app.domain.errors import DomainError
from app.domain.faceit import FaceitLookup, FaceitStats
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
    monkeypatch.setattr("app.telegram.routers.faceit.time", lambda: now)

    async def fetch(lookup):
        lookups.append(lookup)
        if lookup.kind == "player_id":
            raise DomainError("Статистика FACEIT временно недоступна.")
        return FaceitStats(PLAYER_ID, "PartyUp-Test", 9, 1891, 1200, 654, 54.5)

    harness = Harness(service, faceit=fetch)
    await harness.update(1001, "/faceit")
    await harness.update(1001, callback="faceit:steam")
    text = harness.sent()[-1].text
    assert "Уровень FACEIT: 9/10" in text
    assert "Elo: 1891" in text
    assert "Матчи CS2: 1200" in text

    await harness.update(1001, "/cs2")
    await harness.update(1001, callback="faceit:refresh")
    assert lookups == [FaceitLookup("steam_id", STEAM_ID)]

    with service.db.session.begin() as session:
        session.get(FaceitProfile, user.id).checked_at = utcnow() - timedelta(minutes=16)
    now += 20
    await harness.update(1001, callback="faceit:refresh")
    assert lookups[-1] == FaceitLookup("player_id", PLAYER_ID)
    assert "временно недоступна" in harness.sent()[-1].text
    assert service.faceit.get(user.id).elo == 1891

    await harness.update(1001, callback="faceit:disconnect")
    assert service.faceit.get(user.id) is None

    now += 20
    await harness.update(1001, "/cs2 PartyUp-Test")
    assert lookups[-1] == FaceitLookup("nickname", "PartyUp-Test")
    assert service.faceit.get(user.id)
    await harness.update(1001, "/delete")
    await harness.update(1001, callback="delete:yes")
    assert service.faceit.get(user.id) is None
