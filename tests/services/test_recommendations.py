from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from app.domain.matching import compatible, schedule_slots, score
from app.domain.profiles import ProfileInput


@pytest.mark.parametrize("values", [
    {"age": 17}, {"age_min": 30, "age_max": 20}, {"timezone": "Mars/Olympus"},
    {"start_hour": 10, "end_hour": 10}, {"game_ids": []}, {"name": "  "},
])
def test_invalid_profiles_rejected(values):
    base = {"name": "Alex", "age": 25, "game_ids": [105600]}
    base.update(values)
    with pytest.raises(ValidationError):
        ProfileInput(**base)


def test_shared_games_outweigh_timezone_only(service, users):
    actor = users(1)
    common = users(2, game_ids=[105600, 548430], timezone="America/New_York")
    different = users(3, game_ids=[730, 570])
    assert score(actor, common)["score"] > score(actor, different)["score"]


def test_new_games_content_fallback(service, users):
    a = users(1, game_ids=[3241660])
    b = users(2, game_ids=[3241660])
    result = score(a, b)
    assert result["components"]["main_game"] > 0
    assert result["common_games"] == ["R.E.P.O."]


def test_timezone_and_overnight_are_real_utc_intervals(service, users):
    now = datetime(2026, 11, 17, tzinfo=timezone.utc)
    a = users(1, timezone="Asia/Novosibirsk", start_hour=22, end_hour=2)
    b = users(2, timezone="Europe/Moscow", start_hour=18, end_hour=22)
    assert schedule_slots(a, now) == schedule_slots(b, now)
    assert score(a, b, now)["overlap_hours"] == 4


def test_dst_applied_per_date(users):
    berlin = users(1, timezone="Europe/Berlin", start_hour=19, end_hour=23)
    utc = users(2, timezone="UTC", start_hour=18, end_hour=22)
    winter = datetime(2026, 11, 17, tzinfo=timezone.utc)
    summer = datetime(2026, 6, 17, tzinfo=timezone.utc)
    assert schedule_slots(berlin, winter) == schedule_slots(utc, winter)
    assert schedule_slots(berlin, summer) != schedule_slots(utc, summer)


def test_mutual_filters_and_single_purpose_field(users):
    a = users(1, gender="male", looking_for="female", age_min=20, age_max=30, purpose="romance")
    b = users(2, gender="female", looking_for="male", age_min=18, age_max=23, purpose="both")
    assert not compatible(a, b)  # b rejects a's age.
    b.age_max = 30
    assert compatible(a, b)
    b.purpose = "friends"
    assert not compatible(a, b)


def test_empty_library_does_not_match_by_unused_purchases(service, users):
    a, b = users(1), users(2)
    for entry in a.games:
        entry.selected = False
        entry.minutes = 0
    result = score(a, b)
    assert result["components"]["games"] == 0
    assert result["components"]["genres"] == 0


def test_feed_prioritizes_mutual_dota_playtime_and_concentration(service, users):
    actor, dedicated, broad, new_player, manual = (
        users(telegram, game_ids=[570]) for telegram in range(1, 6)
    )

    def import_hours(user, games):
        service.steam.import_games(user.id, [{"appid": app_id, "playtime_forever": hours * 60}
                                       for app_id, hours in games.items()])

    import_hours(actor, {570: 1500, 730: 0})
    import_hours(dedicated, {570: 1200, 730: 30})
    import_hours(broad, {570: 1500, 730: 4500})
    import_hours(new_player, {570: 2})

    rows = service.discovery.feed(actor.id)
    assert rows[0]["profile"]["id"] == dedicated.id
    by_id = {row["profile"]["id"]: row for row in rows}
    assert by_id[dedicated.id]["score"] > by_id[manual.id]["score"] > by_id[new_player.id]["score"]
    assert by_id[dedicated.id]["score"] > by_id[broad.id]["score"]
    assert by_id[broad.id]["common_games"] == ["Dota 2"]
    assert by_id[broad.id]["components"]["other_games"] == 0


def test_secondary_shared_game_contributes_with_both_players_hours(service, users):
    a, b = users(1, game_ids=[570]), users(2, game_ids=[570])
    for user, hours in ((a, 200), (b, 100)):
        service.steam.import_games(user.id, [
            {"appid": 570, "playtime_forever": 600 * 60}, {"appid": 730, "playtime_forever": hours * 60},
        ])
    result = score(service.profiles.user(a.id), service.profiles.user(b.id))
    assert result["common_games"] == ["Dota 2", "Counter-Strike 2"]
    assert 0 < result["components"]["other_games"] < result["components"]["main_game"]
