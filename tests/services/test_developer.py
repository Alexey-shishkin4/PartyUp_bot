import json

import pytest
from sqlalchemy import func, select

from app.config import Settings
from app.domain.errors import DomainError
from app.persistence.models import Event, Match, Reaction, User
from app.services.developer import DeveloperAccessDenied, DeveloperService
from app.services.fixture_catalog import fixture_definitions
from app.services.fixtures import seed_test_profiles


def developer(service):
    return DeveloperService(service, Settings(_env_file=None, developer_ids="1"))


@pytest.mark.parametrize(
    "operation",
    [
        lambda dev: dev.stats(9),
        lambda dev: dev.list_profiles(9),
        lambda dev: dev.read_profile(9, 1),
        lambda dev: dev.update_profile(9, 1, {"name": "Нельзя"}),
        lambda dev: dev.set_games(9, 1, [105600]),
        lambda dev: dev.set_photo(9, 1, "bad-photo"),
        lambda dev: dev.preview(9, 1),
        lambda dev: dev.compare(9, 1, 2),
        lambda dev: dev.reset_tests(9),
        lambda dev: dev.history(9),
    ],
)
def test_every_developer_operation_requires_authorization(service, operation):
    with pytest.raises(DeveloperAccessDenied):
        operation(developer(service))


def test_profiles_include_hidden_banned_and_test_with_pagination(service, users):
    first, second = users(1), users(2)
    with service.db.session.begin() as session:
        session.get(User, first.id).active = False
        session.get(User, second.id).banned = True
    seed_test_profiles(service)
    dev = developer(service)
    page = dev.list_profiles(1)
    total = len(fixture_definitions()) + 2
    assert page["total"] == total and len(page["profiles"]) == 5
    assert len(dev.list_profiles(1, offset=5)["profiles"]) == 5
    assert len(dev.list_profiles(1, offset=total - 2)["profiles"]) == 2
    assert dev.list_profiles(1, scope="paused")["profiles"][0]["id"] == first.id
    assert dev.list_profiles(1, scope="banned")["profiles"][0]["id"] == second.id
    assert dev.list_profiles(1, scope="test")["total"] == len(fixture_definitions())
    assert dev.list_profiles(1, scope="real")["total"] == 2
    assert dev.read_profile(1, second.id)["banned"]


def test_field_edits_validate_preserve_related_data_and_audit_only_field_names(service, users, steam_library):
    target = users(2)
    service.profiles.set_photo(target.id, "keep-photo")
    steam_library(target.id, [{"appid": 570, "name": "Dota 2", "playtime_forever": 600}])
    dev = developer(service)
    dev.update_profile(1, target.id, {"name": "Новое имя", "bio": "Private updated biography", "age": 27})
    updated = service.profiles.user(target.id)
    assert updated.name == "Новое имя" and updated.age == 27 and updated.photo_file_id == "keep-photo"
    assert updated.telegram_id == target.telegram_id
    assert any(entry.app_id == 570 and entry.minutes == 600 for entry in updated.games)
    rows = dev.history(1)
    assert rows[0].actor_telegram_id == 1 and rows[0].target_user_id == target.id
    assert rows[0].details == {"fields": ["age", "bio", "name"]}
    assert "Private updated biography" not in json.dumps(rows[0].details)


@pytest.mark.parametrize(
    "changes",
    [
        {"age": 17},
        {"age_min": 35, "age_max": 20},
        {"timezone": "Mars/Test"},
        {"telegram_id": 9},
        {"is_test": False},
        {"steam_id": "76561198000000001"},
        {"banned": "false"},
        {"start_hour": 22, "end_hour": 22},
    ],
)
def test_invalid_or_identity_changes_rejected_atomically(service, users, changes):
    target = users(2)
    with pytest.raises(DomainError):
        developer(service).update_profile(1, target.id, changes)
    assert service.profiles.user(target.id).age == target.age
    assert developer(service).history(1) == []


def test_editing_banned_profile_and_unbanning_is_allowed_to_developer(service, users):
    target = users(2)
    dev = developer(service)
    dev.update_profile(1, target.id, {"banned": True})
    dev.update_profile(1, target.id, {"name": "Исправлено"})
    dev.update_profile(1, target.id, {"banned": False, "active": False})
    updated = service.profiles.user(target.id)
    assert updated.name == "Исправлено" and not updated.banned and not updated.active


def test_game_edits_preserve_imported_hours(service, users, steam_library):
    target = users(2)
    steam_library(target.id, [{"appid": 570, "name": "Dota 2", "playtime_forever": 500}])
    dev = developer(service)
    dev.set_games(1, target.id, [570, 730])
    updated = service.profiles.user(target.id)
    assert {entry.app_id for entry in updated.games if entry.selected} == {570, 730}
    assert next(entry for entry in updated.games if entry.app_id == 570).minutes == 500
    with pytest.raises(DomainError):
        dev.set_games(1, target.id, [])
    with pytest.raises(DomainError):
        dev.set_games(1, target.id, [99999999])


def test_developer_fixture_edits_and_photo_overrides_survive_seeding(service):
    seed_test_profiles(service)
    target = service.profiles.user_by_telegram(next(iter(fixture_definitions())))
    dev = developer(service)
    dev.update_profile(1, target.id, {"name": "Тест после редактирования"})
    dev.set_games(1, target.id, [570])
    dev.set_photo(1, target.id, "developer-photo")
    seed_test_profiles(service)
    updated = service.profiles.user(target.id)
    assert updated.name == "Тест после редактирования"
    assert [entry.app_id for entry in updated.games if entry.selected] == [570]
    assert updated.photo_file_id == "developer-photo" and updated.test_photo_override
    assert service.profiles.test_photo_file(target.id) is None
    dev.set_photo(1, target.id, None)
    seed_test_profiles(service)
    assert service.profiles.photo_file(target.id) is None
    assert service.profiles.test_photo_file(target.id) is None


def test_readonly_preview_does_not_change_activity_or_impressions(service, users):
    viewer = users(2)
    users(3)
    before = service.profiles.user(viewer.id).last_active
    result = developer(service).preview(1, viewer.id)
    assert len(result) == 1
    assert service.profiles.user(viewer.id).last_active == before
    with service.db.session() as session:
        assert session.scalar(select(func.count()).select_from(Event)) == 0
    assert developer(service).compare(1, viewer.id, result[0]["profile"]["id"])["compatible"]


def test_reset_affects_only_developers_own_test_reactions(service, users):
    me, peer = users(1), users(2)
    seed_test_profiles(service)
    service.discovery.feed(me.id)
    service.discovery.feed(peer.id)
    service.discovery.react(me.id, peer.id, "like")
    service.discovery.react(peer.id, me.id, "like")
    target = service.discovery.feed(me.id, test_only=True)[0]["profile"]["id"]
    service.discovery.react(me.id, target, "skip")
    other = service.discovery.feed(peer.id, test_only=True)[0]["profile"]["id"]
    service.discovery.react(peer.id, other, "skip")
    developer(service).reset_tests(1)
    with service.db.session() as session:
        assert session.get(Reaction, (me.id, target)) is None
        assert session.get(Reaction, (peer.id, other)) is not None
        assert session.scalar(select(func.count()).select_from(Match)) == 1
    assert developer(service).history(1)[0].action == "test_reset"


def test_permission_revocation_takes_effect_on_next_operation(service, users):
    target = users(2)
    dev = developer(service)
    assert dev.read_profile(1, target.id)
    dev.settings.developer_ids = ""
    with pytest.raises(DeveloperAccessDenied):
        dev.update_profile(1, target.id, {"name": "Forbidden"})
    assert service.profiles.user(target.id).name == target.name


def test_diagnostics_never_return_tokens_or_api_keys(service):
    settings = Settings(
        _env_file=None,
        developer_ids="1",
        bot_token="123456789:SECRET_BOT_TOKEN",
        steam_api_key="SECRET_STEAM_API",
    )
    data = DeveloperService(service, settings).stats(1)
    assert data["steam_api_configured"]
    assert "SECRET" not in json.dumps(data)


def test_admins_can_also_use_developer_panel(service):
    dev = DeveloperService(service, Settings(_env_file=None, admin_ids="7"))
    assert dev.stats(7)["profiles"] == 0
