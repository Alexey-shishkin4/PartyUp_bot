from datetime import timedelta

import pytest

from app.domain.errors import DomainError
from app.domain.riot import LEAGUE_SOLO, VALORANT_ACT, VALORANT_RANKED_ACTS, RiotGame, RiotId, RiotStats
from app.persistence.models import RiotProfile, utcnow
from tests.telegram.harness import Harness


@pytest.mark.asyncio
async def test_one_riot_id_links_both_games_and_disconnect_removes_both(service, users):
    user = users(1001)
    calls = []

    async def fetch_riot(game, region, riot_id):
        calls.append((game, region, riot_id))
        if game is RiotGame.LEAGUE:
            return RiotStats(riot_id, "Gold II", 19, LEAGUE_SOLO, "iodid", "euw1")
        return RiotStats(riot_id, "Diamond 1", 24, VALORANT_RANKED_ACTS, "ugi", "eu")

    harness = Harness(service, riot=fetch_riot)
    await harness.update(1001, "/games")
    await harness.update(1001, callback="accounts:choose:riot")
    assert "<code>Имя#Тег</code>" in harness.sent()[-1].text
    await harness.update(1001, "<Main>#EUW")
    await harness.update(1001, "/profile")
    text = harness.sent()[-1].text
    assert "Valorant (Diamond 1)" in text and "League of Legends (Gold II)" in text
    assert "&lt;Main&gt;#EUW" not in text
    assert service.riot.get(user.id, RiotGame.VALORANT).region == "eu"
    assert service.riot.get(user.id, RiotGame.VALORANT).match_count == 24
    assert service.riot.get(user.id, RiotGame.LEAGUE).match_count == 19
    await harness.update(1001, callback="accounts:refresh:riot")
    assert set(calls) == {
        (RiotGame.VALORANT, None, RiotId("<Main>", "EUW")),
        (RiotGame.LEAGUE, None, RiotId("<Main>", "EUW")),
    }
    await harness.update(1001, callback="accounts:disconnect:riot")
    assert service.riot.get(user.id, RiotGame.VALORANT) is None
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
        assert riot_id == RiotId("Jett Main", "EUW")
        assert region == ("eu" if game is RiotGame.VALORANT else None)
        raise DomainError("Статистику Valorant временно не удалось получить.")

    harness = Harness(service, riot=fetch_riot)
    await harness.update(1001, callback="accounts:refresh:riot")
    assert "временно не удалось получить" in harness.sent()[-1].text
    await harness.update(1001, "/profile")
    assert "Valorant (Gold 1)" in harness.sent()[-1].text
    assert service.riot.get(user.id, RiotGame.VALORANT).match_count == 18


@pytest.mark.asyncio
async def test_legacy_act_snapshot_can_be_refreshed_immediately(service, users):
    user = users(1001)
    riot_id = RiotId("Jett Main", "EUW")
    service.riot.save_account(
        user.id,
        riot_id,
        {RiotGame.VALORANT: RiotStats(riot_id, "Gold 1", None, VALORANT_ACT, "henrik", "eu")},
        {game: None for game in RiotGame},
    )

    async def fetch_riot(game, region, account):
        if game is RiotGame.LEAGUE:
            raise DomainError("Профиль League of Legends не найден.")
        assert (game, region, account) == (RiotGame.VALORANT, "eu", riot_id)
        return RiotStats(account, "Gold 1", 187, VALORANT_RANKED_ACTS, "henrik", "eu")

    harness = Harness(service, riot=fetch_riot)
    await harness.update(1001, callback="accounts:refresh:riot")
    assert service.riot.get(user.id, RiotGame.VALORANT).match_count == 187
    assert service.riot.get(user.id, RiotGame.VALORANT).match_scope == VALORANT_RANKED_ACTS
    assert service.riot.get(user.id, RiotGame.LEAGUE) is None


async def test_legacy_separate_ids_are_preserved_until_user_changes_the_account(service, users, monkeypatch):
    user = users(1)
    with service.db.session.begin() as session:
        for game, name, region, scope in (
            (RiotGame.LEAGUE, "FirstOne", "ru", LEAGUE_SOLO),
            (RiotGame.VALORANT, "SecondOne", "eu", VALORANT_RANKED_ACTS),
        ):
            session.add(RiotProfile(
                user_id=user.id, game=game, game_name=name, tag_line="TAG", region=region,
                match_scope=scope, rank="Gold I", source="henrik", checked_at=utcnow() - timedelta(hours=1),
            ))

    clock = 100
    monkeypatch.setattr("app.telegram.routers.game_accounts.time", lambda: clock)
    calls = []

    async def fetch(game, region, riot_id):
        calls.append((game, region, riot_id))
        if game is RiotGame.VALORANT:
            raise DomainError("Профиль не найден.")
        return RiotStats(riot_id, "Diamond I", 10, LEAGUE_SOLO, "opgg", "ru")

    harness = Harness(service, riot=fetch)
    await harness.update(1, callback="accounts:refresh:riot")
    assert "разные Riot ID" in harness.sent()[-1].text and not calls
    assert service.riot.get(user.id, RiotGame.VALORANT).game_name == "SecondOne"
    clock += 20
    await harness.update(1, callback="accounts:change:riot")
    await harness.update(1, "NewOne#TAG")
    assert service.riot.get(user.id, RiotGame.LEAGUE).game_name == "NewOne"
    assert service.riot.get(user.id, RiotGame.VALORANT) is None
