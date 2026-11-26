import pytest
from sqlalchemy import func, select

from app.domain.deadlock import DeadlockStats
from app.domain.faceit import FaceitLookup, FaceitStats
from app.domain.fortnite import Activity, EpicAccount, FortniteSnapshot, Playtime, Rank
from app.domain.pubg import PubgAccount, PubgPlaytime, PubgRanking, PubgStats
from app.domain.riot import LEAGUE_SOLO, VALORANT_RANKED_ACTS, RiotGame, RiotId, RiotStats
from app.domain.steam import CS2_APP_ID, DEADLOCK_APP_ID, PUBG_APP_ID, PublicProfile
from app.persistence.models import GameAccount, GameStats, utcnow
from tests.telegram.harness import Harness

STEAM_ID = "76561198000000001"


async def reach_game_source(harness):
    await harness.update(1, "/start")
    await harness.update(1, "Игрок")
    await harness.update(1, "25")
    await harness.update(1, callback="form:timezone:Asia/Novosibirsk")
    await harness.update(1, callback="form:bio:-")
    await harness.update(1, callback="photo:default")


async def test_steam_link_automatically_saves_ranks_then_offers_pubg_and_one_riot_id(service, users):
    user = users(1)
    riot_calls = []
    pubg_calls = []

    class Steam:
        async def fetch(self, url):
            return PublicProfile(STEAM_ID, url, [
                {"appid": CS2_APP_ID, "playtime_forever": 600},
                {"appid": DEADLOCK_APP_ID, "playtime_2weeks": 60},
                {"appid": PUBG_APP_ID, "playtime_forever": 600, "name": "PUBG: BATTLEGROUNDS"},
            ], "library")

    async def faceit(lookup):
        assert lookup == FaceitLookup("steam_id", STEAM_ID)
        return FaceitStats("faceit-account", "Faceit-Nickname", 4, 901, 239, 118, 49, steam_id=STEAM_ID)

    async def deadlock(steam_id):
        assert steam_id == STEAM_ID
        return DeadlockStats(93, None, 5, 6060)

    async def pubg(platform, identity, **_):
        pubg_calls.append((platform, identity))
        return PubgStats(
            PubgAccount("account." + "b" * 32, identity, platform),
            PubgPlaytime({"squad-fpp": 3600}, "public"),
            PubgRanking({"squad-fpp": "Gold 3 · 1785 RP"}, "official", "division.bro.official.pc-2018-44"),
        )

    async def riot(game, region, account):
        riot_calls.append((game, region, account))
        if game is RiotGame.LEAGUE:
            return RiotStats(account, "Gold II", 19, LEAGUE_SOLO, "opgg", "ru")
        return RiotStats(account, "Platinum 3", 24, VALORANT_RANKED_ACTS, "henrik", "eu")

    async def fortnite(*args, **kwargs):
        raise AssertionError("Fortnite must be offered as a manual link")

    harness = Harness(service, Steam(), faceit=faceit, deadlock=deadlock, pubg=pubg, riot=riot, fortnite=fortnite)
    await harness.update(1, "/steam")
    await harness.update(1, f"https://steamcommunity.com/profiles/{STEAM_ID}")
    assert "Пришли ник или Account ID PUBG" in harness.sent()[-1].text
    assert service.faceit.get(user.id).details["elo"] == 901
    assert service.deadlock.get(user.id, STEAM_ID).details["match_seconds"] == 6060
    assert not pubg_calls and not riot_calls

    await harness.update(1, "PubgPlayer")
    assert "Подключить Riot?" in harness.sent()[-1].text
    assert pubg_calls == [("steam", "PubgPlayer")]
    await harness.update(1, callback="accounts:riot_yes")
    await harness.update(1, "Riot Player#EUW")
    assert len(riot_calls) == 2
    assert set(riot_calls) == {(game, None, RiotId("Riot Player", "EUW")) for game in RiotGame}
    markup = repr(harness.sent()[-1].reply_markup)
    assert "accounts:choose:fortnite" in markup
    assert all(f"accounts:choose:{name}" not in markup for name in ("pubg", "riot", "faceit", "deadlock"))

    await harness.update(1, "/profile")
    card = harness.sent()[-1].text
    for game in ("Counter-Strike 2 (FACEIT 4)", "Deadlock (Phantom 3)", "PUBG (Gold 3)",
                 "League of Legends (Gold II)", "Valorant (Platinum 3)"):
        assert game in card
    for private in (STEAM_ID, "PubgPlayer", "Riot Player", "Faceit-Nickname", "1785 RP", "901", "239"):
        assert private not in card


async def test_registration_can_skip_steam_and_riot_then_return_to_add_games(service):
    async def unexpected_fetch(*args, **kwargs):
        raise AssertionError("Skipping must not make provider requests")

    harness = Harness(service, riot=unexpected_fetch, pubg=unexpected_fetch)
    await reach_game_source(harness)
    await harness.update(1, callback="registration:skip")
    await harness.update(1, callback="review:publish")

    user = service.profiles.user_by_telegram(1)
    assert user and not user.steam_id and not user.games
    assert "Подключить Riot?" in harness.sent()[-1].text
    await harness.update(1, callback="accounts:riot_skip")
    markup = repr(harness.sent()[-1].reply_markup)
    assert all(value in markup for value in ("accounts:choose:pubg", "accounts:choose:riot", "steam:open"))
    await harness.update(1, callback="accounts:done")
    await harness.update(1, "➕ Добавить игры")
    assert "accounts:choose:riot" in repr(harness.sent()[-1].reply_markup)


async def test_riot_and_fortnite_appear_in_draft_and_publish_without_refetch(service):
    calls = []

    async def riot(game, region, identity):
        calls.append((game, region, identity))
        if game is RiotGame.LEAGUE:
            return RiotStats(identity, "Gold II", 19, LEAGUE_SOLO, "opgg", "ru")
        return RiotStats(identity, None, 24, VALORANT_RANKED_ACTS, "henrik", "eu")

    async def fortnite(nickname, *, account_id):
        calls.append((nickname, account_id))
        return FortniteSnapshot(
            EpicAccount("a" * 32, nickname),
            utcnow(),
            Playtime(1200, {}, "osirion"),
            Activity({"reload": 120}, {"reload": 8}),
            {"reload_combined": Rank("Champion I", None, "osirion")},
        )

    harness = Harness(service, riot=riot, fortnite=fortnite)
    await reach_game_source(harness)
    markup = repr(harness.sent()[-1].reply_markup)
    assert "draftgame:riot" in markup and "draftgame:fortnite" in markup
    await harness.update(1, callback="draftgame:riot")
    await harness.update(1, "Draft Riot#EUW")
    preview = harness.photos_sent()[-1]
    assert "League of Legends (Gold II)" in preview.caption and "• Valorant" in preview.caption
    await harness.update(1, callback="draftgame:fortnite")

    restarted = Harness(service, riot=riot, fortnite=fortnite)
    await restarted.update(1, "/start")
    assert "имя Epic Games" in restarted.sent()[-1].text
    await restarted.update(1, "Draft Epic")
    preview = restarted.photos_sent()[-1].caption
    assert "Fortnite (Champion I)" in preview and "League of Legends (Gold II)" in preview
    assert "Draft Epic" not in preview and "Draft Riot" not in preview
    assert service.profiles.user_by_telegram(1) is None
    with service.db.session() as session:
        assert session.scalar(select(func.count()).select_from(GameAccount)) == 0
        assert session.scalar(select(func.count()).select_from(GameStats)) == 0

    await restarted.update(1, callback="review:publish")
    assert len(calls) == 3
    user = service.profiles.user_by_telegram(1)
    assert user is not None and not user.steam_id
    assert service.riot.get(user.id, RiotGame.VALORANT).matches_count == 24
    assert service.riot.get(user.id, RiotGame.LEAGUE).rank == "Gold II"
    assert service.fortnite.get(user.id).playtime_minutes == 1200
    assert "Подключить Riot?" not in restarted.sent()[-1].text
    assert "accounts:choose:fortnite" not in repr(restarted.sent()[-1].reply_markup)


@pytest.mark.parametrize("manual_games", [False, True])
async def test_removing_fortnite_from_draft_keeps_manual_games_and_excludes_account(service, manual_games):
    async def fetch_fortnite(nickname, *, account_id):
        return FortniteSnapshot(EpicAccount("a" * 32, nickname), utcnow())

    harness = Harness(service, fortnite=fetch_fortnite)
    await reach_game_source(harness)
    if manual_games:
        await harness.update(1, callback="registration:manual")
        await harness.update(1, callback="game:105600")
        await harness.update(1, callback="game:save")
    await harness.update(1, callback="draftgame:fortnite")
    await harness.update(1, "Draft Epic")
    assert "• Fortnite" in harness.photos_sent()[-1].caption
    await harness.update(1, callback="draftgame:fortnite")
    await harness.update(1, callback="draftgame:remove:fortnite")
    if manual_games:
        assert "• Terraria" in harness.photos_sent()[-1].caption
    else:
        assert "registration:skip" in repr(harness.sent()[-1].reply_markup)
        await harness.update(1, callback="registration:skip")
    assert "• Fortnite" not in harness.photos_sent()[-1].caption
    await harness.update(1, callback="review:publish")
    user = service.profiles.user_by_telegram(1)
    assert user is not None and service.fortnite.get(user.id) is None


@pytest.mark.parametrize("remove_faceit", [False, True])
async def test_steam_faceit_deadlock_and_pubg_can_be_linked_before_publication(service, remove_faceit):
    calls = []

    class Steam:
        async def fetch(self, url):
            calls.append("steam")
            return PublicProfile(STEAM_ID, url, [{"appid": CS2_APP_ID, "playtime_forever": 600}], "library")

    async def faceit(lookup):
        calls.append("faceit")
        assert lookup == FaceitLookup("steam_id", STEAM_ID)
        return FaceitStats("faceit-id", "Private Nickname", 4, 901, 239, 118, 49, steam_id=STEAM_ID)

    async def deadlock(steam_id):
        calls.append("deadlock")
        assert steam_id == STEAM_ID
        return DeadlockStats(93, None, 5, 6060)

    async def pubg(platform, identity, *, account_id, need_playtime, need_rank):
        calls.append("pubg")
        assert (platform, identity, account_id, need_playtime, need_rank) == ("steam", "MyPUBG", None, True, True)
        return PubgStats(
            PubgAccount("account." + "b" * 32, identity, platform),
            PubgPlaytime({"squad-fpp": 3600}, "public"),
            PubgRanking({"squad-fpp": "Gold 3 · 1785 RP"}, "official"),
        )

    harness = Harness(service, Steam(), pubg=pubg, faceit=faceit, deadlock=deadlock)
    await reach_game_source(harness)
    await harness.update(1, callback="draftgame:deadlock")
    assert "понадобится ссылка на Steam" in harness.sent()[-2].text
    await harness.update(1, f"https://steamcommunity.com/profiles/{STEAM_ID}")
    preview = harness.photos_sent()[-1].caption
    assert "Counter-Strike 2 (FACEIT 4)" in preview and "Deadlock (Phantom 3)" in preview
    if remove_faceit:
        await harness.update(1, callback="draftgame:faceit")
        await harness.update(1, callback="draftgame:remove:faceit")
        assert "FACEIT 4" not in harness.photos_sent()[-1].caption

    await harness.update(1, callback="draftgame:pubg")
    await harness.update(1, "MyPUBG")
    assert "PUBG (Gold 3)" in harness.photos_sent()[-1].caption
    assert service.profiles.user_by_telegram(1) is None

    await harness.update(1, callback="review:publish")
    assert calls == ["steam", "faceit", "deadlock", "pubg"]
    user = service.profiles.user_by_telegram(1)
    assert user.steam_id == STEAM_ID
    assert (service.faceit.get(user.id) is None) == remove_faceit
    assert service.deadlock.get(user.id, STEAM_ID).details["match_seconds"] == 6060
    assert service.pubg.get(user.id).playtime_minutes == 60
