from app.domain.deadlock import DeadlockStats
from app.domain.faceit import FaceitLookup, FaceitStats
from app.domain.pubg import PubgAccount, PubgPlaytime, PubgRanking, PubgStats
from app.domain.riot import LEAGUE_SOLO, VALORANT_RANKED_ACTS, RiotGame, RiotId, RiotStats
from app.domain.steam import CS2_APP_ID, DEADLOCK_APP_ID, PUBG_APP_ID, PublicProfile
from tests.telegram.harness import Harness

STEAM_ID = "76561198000000001"


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
    assert service.faceit.get(user.id).elo == 901
    assert service.deadlock.get(user.id, STEAM_ID).match_seconds == 6060
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
    await harness.update(1, "/start")
    await harness.update(1, "Игрок")
    await harness.update(1, "25")
    await harness.update(1, callback="form:timezone:Asia/Novosibirsk")
    await harness.update(1, callback="form:bio:-")
    await harness.update(1, callback="photo:default")
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
