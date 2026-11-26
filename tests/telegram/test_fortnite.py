from datetime import timedelta

import pytest

from app.domain.errors import DomainError
from app.domain.fortnite import Activity, EpicAccount, FortniteSnapshot, Playtime, Rank
from app.persistence.models import GameStats, utcnow
from tests.telegram.harness import Harness


def snapshot(nickname="Test Player"):
    return FortniteSnapshot(
        EpicAccount("a" * 32, nickname),
        utcnow(),
        Playtime(1200, {"zero_build": 300}, "osirion"),
        Activity({"battle_royale": 30, "zero_build": 75}, {"zero_build": 6}),
        {"zero_build": Rank("Gold I", "s1", "osirion")},
    )


async def test_fortnite_adds_only_game_and_rank_to_card_and_disconnect_removes_modes(service, users):
    user = users(1)

    async def fetch(nickname, **_):
        return snapshot(nickname)

    harness = Harness(service, fortnite=fetch)
    try:
        await harness.update(1, "/games")
        await harness.update(1, callback="accounts:choose:fortnite")
        await harness.update(1, "Test Player")
        await harness.update(1, "/profile")
        text = harness.sent()[-1].text
        assert "Fortnite (Gold I)" in text
        assert all(value not in text for value in ("Test Player", "20 ч", "Zero Build", "30 дней"))
        saved = service.fortnite.get(user.id)
        assert saved.details["favorite_mode"] == "zero_build" and saved.playtime_minutes == 1200
        await harness.update(1, callback="accounts:disconnect:fortnite")
        assert service.fortnite.get(user.id) is None
        with service.db.session() as session:
            assert session.get(GameStats, saved.id) is None
        with pytest.raises(DomainError, match="отключён или изменён"):
            service.fortnite.save(user.id, snapshot(), expected_account_id="a" * 32)
    finally:
        await harness.bot.session.close()


def test_partial_refresh_preserves_known_playtime_ranks_and_the_date_of_activity(service, users):
    user = users(1)
    data = snapshot()
    service.fortnite.save(user.id, data)
    saved = service.fortnite.save(
        user.id,
        FortniteSnapshot(data.account, data.checked_at + timedelta(hours=1)),
        expected_account_id=data.account.account_id,
    )
    assert saved.playtime_minutes == 1200
    assert saved.details["recent_checked_at"] == data.checked_at.isoformat()
    assert saved.details["favorite_mode"] == "zero_build"
    assert saved.details["ranks"]["zero_build"]["rank"] == "Gold I"
    service.profiles.delete_user(user.telegram_id)
    assert service.fortnite.get(user.id) is None
    with service.db.session() as session:
        assert session.get(GameStats, saved.id) is None
