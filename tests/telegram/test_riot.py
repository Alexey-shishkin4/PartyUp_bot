from datetime import timedelta

import pytest

from app.domain.errors import DomainError
from app.domain.riot import LEAGUE_SOLO, VALORANT_ACT, VALORANT_RANKED_ACTS, RiotGame, RiotId, RiotStats
from app.persistence.models import RiotProfile, utcnow
from tests.telegram.harness import Harness


@pytest.mark.asyncio
async def test_riot_commands_save_separate_profiles_and_delete_with_user(service, users):
    user = users(1001)
    calls = []

    async def fetch_riot(game, region, riot_id):
        calls.append((game, region, riot_id))
        if game is RiotGame.LEAGUE:
            return RiotStats(riot_id, "Gold II", 19, LEAGUE_SOLO, "riot", "euw1")
        return RiotStats(riot_id, "Diamond 1", 24, VALORANT_RANKED_ACTS, "ugi", "eu")

    harness = Harness(service, riot=fetch_riot)
    await harness.update(1001, "/valorant")
    assert "свой Riot ID" in harness.sent()[-1].text
    assert "<code>Имя#Тег</code>" in harness.sent()[-1].text
    await harness.update(1001, "Jett Main#EUW")
    assert "Рейтинговые матчи за все доступные акты: 24" in harness.sent()[-1].text
    assert service.riot.get(user.id, RiotGame.VALORANT).region == "eu"
    await harness.update(1001, "/lol <Main>#EUW")
    assert "&lt;Main&gt;#EUW" in harness.sent()[-1].text
    assert "Матчи в Solo/Duo: 19" in harness.sent()[-1].text
    await harness.update(1001, callback="riot:lol:refresh")
    await harness.update(1001, "/lol euw <Main>#EUW")
    assert "&lt;Main&gt;#EUW" in harness.sent()[-1].text
    assert calls == [
        (RiotGame.VALORANT, None, RiotId("Jett Main", "EUW")),
        (RiotGame.LEAGUE, None, RiotId("<Main>", "EUW")),
    ]
    await harness.update(1001, callback="riot:valorant:disconnect")
    assert service.riot.get(user.id, RiotGame.VALORANT) is None
    assert service.riot.get(user.id, RiotGame.LEAGUE).rank == "Gold II"
    await harness.update(1001, "/delete")
    await harness.update(1001, callback="delete:yes")
    assert service.riot.get(user.id, RiotGame.LEAGUE) is None


@pytest.mark.asyncio
async def test_failed_riot_refresh_keeps_last_saved_stats(service, users):
    user = users(1001)
    with service.db.session.begin() as session:
        session.add(
            RiotProfile(
                user_id=user.id,
                game=RiotGame.VALORANT,
                region="eu",
                game_name="Jett Main",
                tag_line="EUW",
                match_scope=VALORANT_ACT,
                match_count=18,
                rank="Gold 1",
                source="ugi",
                checked_at=utcnow() - timedelta(hours=1),
            )
        )

    async def fetch_riot(game, region, riot_id):
        assert (game, region, riot_id) == (RiotGame.VALORANT, "eu", RiotId("Jett Main", "EUW"))
        raise DomainError("Статистику Valorant временно не удалось получить.")

    harness = Harness(service, riot=fetch_riot)
    await harness.update(1001, callback="riot:valorant:refresh")
    assert "временно не удалось получить" in harness.sent()[-1].text
    await harness.update(1001, "/valorant")
    assert "Рейтинговые матчи за текущий акт: 18" in harness.sent()[-1].text


@pytest.mark.asyncio
async def test_legacy_act_snapshot_can_be_refreshed_immediately(service, users):
    user = users(1001)
    riot_id = RiotId("Jett Main", "EUW")
    service.riot.save(
        user.id,
        RiotGame.VALORANT,
        RiotStats(riot_id, "Gold 1", None, VALORANT_ACT, "henrik", "eu"),
        expected_checked_at=None,
    )

    async def fetch_riot(game, region, account):
        assert (game, region, account) == (RiotGame.VALORANT, "eu", riot_id)
        return RiotStats(account, "Gold 1", 187, VALORANT_RANKED_ACTS, "henrik", "eu")

    harness = Harness(service, riot=fetch_riot)
    await harness.update(1001, callback="riot:valorant:refresh")
    assert "Рейтинговые матчи за все доступные акты: 187" in harness.sent()[-1].text
    assert service.riot.get(user.id, RiotGame.VALORANT).match_scope == VALORANT_RANKED_ACTS
