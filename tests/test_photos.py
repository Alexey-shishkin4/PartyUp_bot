import pytest
from sqlalchemy import text

from app.service import DomainError


def test_photo_schema_upgrade_preserves_registered_users(service,users):
    user = users(1)
    with service.db.engine.begin() as connection:
        connection.execute(text("ALTER TABLE users DROP COLUMN photo_file_id"))
        connection.execute(text("ALTER TABLE users DROP COLUMN use_default_avatar"))
        connection.execute(text("ALTER TABLE users DROP COLUMN schedule_set"))
    service.db.initialize()
    restored = service.user(user.id)
    assert restored.name == user.name and restored.telegram_id == user.telegram_id
    assert restored.photo_file_id is None
    assert not restored.use_default_avatar and restored.schedule_set
    assert {entry.app_id for entry in restored.games} == {105600,548430}
    service.set_photo(restored.id,"new-photo-after-upgrade")
    assert service.photo_file(restored.id) == "new-photo-after-upgrade"


def test_invalid_photo_reference_cannot_overwrite_existing_photo(service,users):
    user = users(1)
    service.set_photo(user.id,"existing-photo")
    with pytest.raises(DomainError):
        service.set_photo(user.id,"invalid photo reference")
    assert service.photo_file(user.id) == "existing-photo"
