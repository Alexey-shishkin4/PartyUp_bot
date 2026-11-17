from sqlalchemy import text

from app.bot import profile_text,steam_status_text
from app.models import User


def test_steam_snapshot_appears_without_changing_manual_favorites(service,users):
    user = users(1,game_ids=[1245620])
    service.import_games(user.id,[{"appid":105600,"name":"Terraria","playtime_2weeks":60}],complete=False)
    profile = service.public_profile(service.user(user.id))
    assert profile["games"] == ["ELDEN RING","Terraria"]
    assert profile["selected_games"] == ["ELDEN RING"]
    assert profile["steam_games"] == ["Terraria"]
    assert profile["game_ids"] == [1245620]
    assert profile["steam_import_status"] == "recent"
    assert "Terraria" in profile_text(profile)


def test_large_library_has_bounded_preview_and_total_count(service,users):
    user = users(1)
    service.import_games(user.id,[{"appid":900000+i,"name":f"Imported {i}","playtime_forever":i*60} for i in range(50)])
    profile = service.public_profile(service.user(user.id))
    assert len(profile["games"]) == 10 and len(profile["steam_games"]) == 10
    assert profile["steam_game_count"] == 50 and profile["games_count"] == 52
    assert profile["steam_games"][0] == "Imported 49"
    assert "ещё 42" in profile_text(profile)
    assert "сохранено игр: 50" in steam_status_text(profile)


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
    assert profile["steam_import_status"] == "not_loaded" and profile["steam_games"] == []
    assert "Из Steam" not in profile_text(profile)


def test_known_empty_library_is_distinct_from_failed_check(service,users):
    user = users(1)
    service.import_games(user.id,[])
    profile = service.public_profile(service.user(user.id))
    assert steam_status_text(profile) == "Steam вернул пустую библиотеку."
    with service.db.session.begin() as session:
        session.get(User,user.id).steam_import_status = "profile_only"
    assert "не загрузила список игр" in steam_status_text(service.public_profile(service.user(user.id)))
