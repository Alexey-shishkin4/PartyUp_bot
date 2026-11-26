from alembic import command
from alembic.config import Config
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
