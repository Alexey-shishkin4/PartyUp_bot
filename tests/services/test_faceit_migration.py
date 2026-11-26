from sqlalchemy import text

from app.domain.faceit import FaceitStats


def test_schema_upgrade_preserves_existing_faceit_snapshot(service, users):
    user = users(1001)
    stats = FaceitStats("1efd000a-30e6-408b-8aaa-dd8219860b4b", "PartyUp-Test", 9, 1891, 1200, 654, 54.5)
    service.faceit.save(user.id, stats, expected_checked_at=None)
    with service.db.engine.begin() as connection:
        connection.execute(text("ALTER TABLE faceit_profiles DROP COLUMN steam_id, DROP COLUMN source"))

    service.db.initialize()

    profile = service.faceit.get(user.id)
    assert profile.source == "faceit"
    assert profile.steam_id is None
    assert profile.player_id == stats.player_id
    assert profile.elo == stats.elo
