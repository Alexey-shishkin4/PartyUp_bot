from alembic import command
from alembic.config import Config
import pytest
from sqlalchemy import inspect, text

from app.config import ROOT
from app.domain.faceit import FaceitStats
from app.persistence.database import Database


def test_alembic_schema_matches_models_and_initialization_is_idempotent(service, users):
    user = users(1001)
    stats = FaceitStats("1efd000a-30e6-408b-8aaa-dd8219860b4b", "PartyUp-Test", 9, 1891, 1200, 654, 54.5)
    service.faceit.save(user.id, stats, expected_checked_at=None)
    service.db.initialize()

    with service.db.engine.begin() as connection:
        config = Config(str(ROOT / "alembic.ini"))
        config.attributes["connection"] = connection
        command.check(config)
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "0002"
        schema = inspect(connection)
        assert {"user_games", "game_accounts", "game_stats", "games", "users"} <= set(schema.get_table_names())
        assert "coop" not in {column["name"] for column in schema.get_columns("games")}
    profile = service.faceit.get(user.id)
    assert profile.account.external_account_id == stats.player_id
    assert profile.details["elo"] == stats.elo


def test_alembic_upgrade_preserves_the_existing_game_catalog(postgres_url):
    db = Database(postgres_url)
    try:
        with db.engine.begin() as connection:
            config = Config(str(ROOT / "alembic.ini"))
            config.attributes["connection"] = connection
            command.upgrade(config, "0001")
            connection.execute(text(
                "INSERT INTO games (app_id, name, slug, genres, aliases, coop) "
                "VALUES (730, 'Existing CS2 entry', 'counter-strike-2', '[]', '[]', TRUE)"
            ))
        db.initialize()
        with db.engine.connect() as connection:
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "0002"
            assert connection.scalar(text("SELECT name FROM games WHERE app_id = 730")) == "Existing CS2 entry"
    finally:
        db.engine.dispose()


def test_upgrade_from_pre_alembic_preserves_profiles_and_game_data(service, users, steam_library):
    db = service.db
    user = users(1001)
    steam = steam_library(user.id, [{"appid": 570, "playtime_forever": 240, "playtime_2weeks": 60}])
    with db.engine.begin() as connection:
        config = Config(str(ROOT / "alembic.ini"))
        config.attributes["connection"] = connection
        command.downgrade(config, "0001")
        connection.exec_driver_sql("DROP TABLE game_stats, game_accounts, alembic_version")
        connection.exec_driver_sql("ALTER TABLE games DROP COLUMN slug")
        connection.exec_driver_sql("ALTER TABLE user_games RENAME COLUMN game_id TO app_id")
        connection.exec_driver_sql(
            "ALTER TABLE user_games ALTER COLUMN minutes TYPE INTEGER, "
            "ALTER COLUMN recent_minutes TYPE INTEGER, ALTER COLUMN dota_match_minutes TYPE INTEGER"
        )
        connection.exec_driver_sql(
            "ALTER TABLE users ADD COLUMN steam_id VARCHAR(20), ADD COLUMN steam_checked_at TIMESTAMP, "
            "ADD COLUMN steam_import_status VARCHAR(24) NOT NULL DEFAULT 'not_loaded', "
            "ADD COLUMN dota_rank_tier INTEGER"
        )
        connection.execute(text(
            "UPDATE users SET steam_id = :steam_id, steam_checked_at = CURRENT_TIMESTAMP, "
            "steam_import_status = 'library', dota_rank_tier = 61 WHERE id = :id"
        ), {"id": user.id, "steam_id": steam.steam_id})
        connection.execute(text(
            "UPDATE user_games SET dota_match_minutes = 120 WHERE user_id = :id AND app_id = 570"
        ), {"id": user.id})
        connection.exec_driver_sql(
            "CREATE TABLE riot_profiles (user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE, "
            "game VARCHAR(16) NOT NULL, region VARCHAR(8) NOT NULL, game_name VARCHAR(16) NOT NULL, "
            "tag_line VARCHAR(5) NOT NULL, rank VARCHAR(48), match_count INTEGER, match_scope VARCHAR(24) NOT NULL, "
            "source VARCHAR(16) NOT NULL, checked_at TIMESTAMP NOT NULL, PRIMARY KEY (user_id, game))"
        )
        connection.execute(text(
            "INSERT INTO riot_profiles VALUES "
            "(:id, 'lol', 'ru', 'Older', 'RU', 'Gold I', 54, 'RANKED_SOLO_5x5', 'riot', '2026-11-24'), "
            "(:id, 'valorant', 'eu', 'Latest', 'EU', 'Platinum 3', 33, 'ranked_acts', 'henrik', '2026-11-25')"
        ), {"id": user.id})
        connection.execute(text(
            "INSERT INTO fsm_records VALUES ('draft', :telegram, 'Form:games', json_build_object('selected', ARRAY[570]))"
        ), {"telegram": user.telegram_id})

    db.initialize()
    db.initialize()

    migrated = service.profiles.user(user.id)
    assert migrated.name == user.name
    assert migrated.steam_id == steam.steam_id
    assert migrated.game_account("riot").external_account_id == "latest#eu"
    assert migrated.game_account("riot").details["previous_profiles"][0]["match_count"] == 54
    assert set(service.profiles.public_profile(migrated)["games"]) == {"Dota 2 (Властелин I)", "Valorant (Platinum 3)"}
    with db.engine.connect() as connection:
        config = Config(str(ROOT / "alembic.ini"))
        config.attributes["connection"] = connection
        command.check(config)
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "0002"
        assert connection.scalar(text("SELECT data FROM fsm_records WHERE key = 'draft'")) == {"selected": [570]}
        assert connection.execute(text(
            "SELECT minutes, recent_minutes, dota_match_minutes FROM user_games WHERE user_id = :id AND game_id = 570"
        ), {"id": user.id}).one() == (240, 60, 120)
        assert connection.scalar(text("SELECT playtime_minutes FROM game_stats WHERE game_id = 570")) == 120
        assert "riot_profiles" not in inspect(connection).get_table_names()


def test_unknown_unversioned_schema_is_left_untouched(postgres_url):
    db = Database(postgres_url)
    try:
        with db.engine.begin() as connection:
            connection.exec_driver_sql("CREATE TABLE users (id INTEGER PRIMARY KEY)")
            connection.exec_driver_sql("INSERT INTO users VALUES (17)")
        with pytest.raises(RuntimeError, match="не соответствует поддерживаемой старой схеме"):
            db.initialize()
        with db.engine.connect() as connection:
            assert connection.scalar(text("SELECT id FROM users")) == 17
            assert inspect(connection).get_table_names() == ["users"]
    finally:
        db.engine.dispose()
