from sqlalchemy import text

from app.bot import profile_text,steam_status_text
from app.models import User
from app.recommender import steam_card_game_ids


def test_steam_snapshot_appears_without_changing_manual_favorites(service,users):
    user = users(1,game_ids=[1245620])
    service.import_games(user.id,[{"appid":105600,"name":"Terraria","playtime_2weeks":60}],complete=False)
    profile = service.public_profile(service.user(user.id))
    assert profile["games"] == ["Terraria"]
    assert profile["selected_games"] == ["ELDEN RING"]
    assert profile["game_ids"] == [1245620]
    assert profile["steam_import_status"] == "recent"
    assert "Terraria" in profile_text(profile)


def test_library_card_prioritizes_recent_games_and_keeps_the_full_collection(service,users):
    user = users(1)
    service.import_games(user.id, [
        {"appid": 900000 + i, "name": f"Imported {i}", "playtime_forever": i * 60,
         "playtime_2weeks": 120 if i == 25 else 0} for i in range(50)
    ])
    saved = service.user(user.id)
    profile = service.public_profile(saved)
    assert profile["games"] == ["Imported 25", "Imported 49", "Imported 48", "Imported 47", "Imported 46"]
    assert profile["steam_game_count"] == 50
    assert len([game for game in saved.games if game.imported]) == 50
    viewer = users(2, game_ids=[900003])
    recommendation = service.feed(viewer.id)[0]
    assert recommendation["common_games"] == ["Imported 3"]
    assert "Imported 3" not in profile_text(recommendation["profile"])
    assert "сохранено игр: 50" in steam_status_text(profile)


def test_card_ranking_balances_lifetime_and_recent_hours():
    games = [
        (570, 1500 * 60, 12 * 60),
        (730, 400 * 60, 18 * 60),
        (1245620, 3 * 60, 3 * 60),
        (105600, 200 * 60, 0),
        (440, 0, 0),
    ]
    assert steam_card_game_ids(games) == [570, 730, 105600, 1245620]


def test_card_ranking_uses_recent_time_as_lower_bound_for_lifetime():
    assert steam_card_game_ids([(570, 0, 30 * 60), (730, 60 * 60, 0)]) == [570, 730]


def test_failed_check_preserves_visible_previous_games(service,users):
    user = users(1)
    service.import_games(user.id,[{"appid":570,"name":"Dota 2","playtime_2weeks":120}],complete=False)
    service.mark_steam_checked(user.id)
    profile = service.public_profile(service.user(user.id))
    assert "Dota 2" in profile["games"] and profile["steam_game_count"] == 1
    assert "не загрузила список игр" in steam_status_text(profile)
    assert "из предыдущей загрузки: 1" in steam_status_text(profile)


def test_legacy_imported_rows_visible_after_status_migration(service,users):
    user = users(1)
    service.import_games(user.id,[{"appid":570,"name":"Dota 2","playtime_2weeks":120}],complete=False)
    with service.db.engine.begin() as connection:
        connection.execute(text("ALTER TABLE users DROP COLUMN steam_import_status"))
    service.db.initialize()
    restored = service.user(user.id)
    profile = service.public_profile(restored)
    assert profile["steam_import_status"] == "imported"
    assert "Dota 2" in profile_text(profile)
    assert "сохранено игр: 1" in steam_status_text(profile)


def test_disconnect_clears_status_and_steam_display(service,users):
    user = users(1)
    service.import_games(user.id,[{"appid":570,"name":"Dota 2","playtime_2weeks":120}],complete=False)
    service.disconnect_steam(user.id)
    profile = service.public_profile(service.user(user.id))
    assert profile["steam_import_status"] == "not_loaded" and profile["steam_game_count"] == 0
    assert "Dota 2" not in profile_text(profile)


def test_known_empty_library_is_distinct_from_failed_check(service,users):
    user = users(1)
    service.import_games(user.id,[])
    profile = service.public_profile(service.user(user.id))
    assert steam_status_text(profile) == "Steam вернул пустую библиотеку."
    with service.db.session.begin() as session:
        session.get(User,user.id).steam_import_status = "profile_only"
    assert "не загрузила список игр" in steam_status_text(service.public_profile(service.user(user.id)))
