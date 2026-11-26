from datetime import timedelta

from app.domain.pubg import PubgAccount, PubgPlaytime, PubgRanking, PubgStats
from app.persistence.models import PubgProfile, utcnow
from tests.telegram.harness import Harness


async def test_pubg_saves_both_sections_and_refreshes_only_expired_rank(service, users, monkeypatch):
    user = users(1001)
    account = PubgAccount("account." + "b" * 32, "Example", "steam")
    requests = []
    now = 100.0
    monkeypatch.setattr("app.telegram.routers.pubg.time", lambda: now)

    async def fetch(platform, nickname, *, account_id, need_playtime, need_rank):
        requests.append((platform, nickname, account_id, need_playtime, need_rank))
        playtime = PubgPlaytime({"solo": 3600, "squad-fpp": 5400}, "public") if need_playtime else None
        rank = PubgRanking({"squad-fpp": "Gold 3 · 1785 RP"}, "official", "division.bro.official.pc-2018-44")
        return PubgStats(account, playtime, rank)

    harness = Harness(service, pubg=fetch)
    await harness.update(1001, "/pubg")
    await harness.update(1001, "Example")
    assert requests == [("steam", "Example", None, True, True)]
    card = harness.sent()[-1].text
    assert "Время выживания по доступным режимам: 2 ч 30 мин" in card
    assert "duo-fpp · нет данных" in card
    assert "Ранг текущего сезона 44" in card

    await harness.update(1001, "/pubg")
    await harness.update(1001, callback="pubg:refresh")
    assert len(requests) == 1

    with service.db.session.begin() as session:
        session.get(PubgProfile, user.id).rank_at = utcnow() - timedelta(minutes=16)
    now += 20
    await harness.update(1001, callback="pubg:refresh")
    assert requests[-1] == ("steam", "Example", account.account_id, False, True)
    assert service.pubg.get(user.id).mode_seconds == {"solo": 3600, "squad-fpp": 5400}
    await harness.update(1001, callback="pubg:disconnect")
    assert service.pubg.get(user.id) is None
