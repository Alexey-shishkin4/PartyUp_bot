import pytest

from app.domain.errors import DomainError


def test_database_reinitialization_preserves_registered_users(service,users):
    user = users(1)
    service.profiles.set_photo(user.id, "existing-photo")
    service.db.initialize()
    restored = service.profiles.user(user.id)
    assert restored.name == user.name and restored.telegram_id == user.telegram_id
    assert restored.photo_file_id == "existing-photo"
    assert not restored.use_default_avatar and restored.schedule_set
    assert {entry.app_id for entry in restored.games} == {105600,548430}


def test_invalid_photo_reference_cannot_overwrite_existing_photo(service,users):
    user = users(1)
    service.profiles.set_photo(user.id,"existing-photo")
    with pytest.raises(DomainError):
        service.profiles.set_photo(user.id,"invalid photo reference")
    assert service.profiles.photo_file(user.id) == "existing-photo"
