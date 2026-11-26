from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest
from sqlalchemy import func, select

from app.domain.errors import DomainError
from app.persistence.models import Event, FSMRecord, Match, Reaction, User, utcnow


def mutual(service, a, b):
    service.discovery.feed(a.id)
    service.discovery.feed(b.id)
    service.discovery.react(a.id, b.id, "like")
    return service.discovery.react(b.id, a.id, "like")


def test_mutual_like_is_idempotent_under_concurrency(service, users):
    a, b = users(1), users(2)
    result = mutual(service, a, b)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: service.discovery.react(a.id, b.id, "like"), range(8)))
    assert result["matched"] and all(row["matched"] and not row["new"] for row in results)
    assert len(service.discovery.matches(a.id)) == 1
    assert not service.discovery.feed(a.id)


def test_cannot_like_arbitrary_unseen_id(service, users):
    a, b = users(1), users(2)
    with pytest.raises(DomainError):
        service.discovery.react(a.id, b.id, "like")


def test_blocks_apply_in_both_directions_and_stop_messages(service, users):
    a, b = users(1), users(2)
    result = mutual(service, a, b)
    service.discovery.react(a.id, b.id, "block")
    assert not service.discovery.matches(a.id) and not service.discovery.matches(b.id)
    with pytest.raises(DomainError):
        service.discovery.match_peer(b.id, result["match_id"])


def test_report_also_blocks(service, users):
    a, b = users(1), users(2)
    service.discovery.feed(a.id)
    service.discovery.react(a.id, b.id, "report", "Спам")
    assert not service.discovery.feed(b.id)


def test_pause_and_inactive_profiles_are_hidden(service, users):
    a, b = users(1), users(2)
    service.profiles.toggle_active(b.id)
    assert not service.discovery.feed(a.id)
    service.profiles.toggle_active(b.id)
    with service.db.session.begin() as session:
        session.get(User, b.id).last_active = utcnow() - timedelta(days=31)
    assert not service.discovery.feed(a.id)


def test_account_deletion_cascades_and_removes_drafts(service, users):
    a, b = users(1), users(2)
    mutual(service, a, b)
    with service.db.session.begin() as session:
        session.add(FSMRecord(key="test", telegram_id=1, state="draft", data={"name":"test"}))
    service.profiles.delete_user(a.telegram_id)
    assert service.profiles.user(a.id) is None
    assert not service.discovery.matches(b.id)
    with service.db.session() as session:
        for model in [Event, Match, Reaction, FSMRecord]:
            assert session.scalar(select(func.count()).select_from(model)) == 0


def test_import_preserves_manual_choices_and_disconnect_removes_import(service, users, steam_library):
    a = users(1)
    steam_library(a.id, [{"appid":105600,"name":"Terraria","playtime_forever":600},
                                {"appid":9999991,"name":"New Game","playtime_forever":500}])
    imported = service.profiles.user(a.id)
    assert len(imported.games) == 3
    assert next(g for g in imported.games if g.app_id == 105600).selected
    service.steam.disconnect_steam(a.id)
    after = service.profiles.user(a.id)
    assert {entry.app_id for entry in after.games} == {105600,548430}
    assert all(entry.minutes == 0 and not entry.imported for entry in after.games)


def test_postgres_stores_unsigned_steam_app_ids_without_overflow(service, users, steam_library):
    user = users(1)
    app_id = 2**32 - 1
    steam_library(user.id, [{"appid": app_id, "name": "New Game", "playtime_forever": 60}])
    service.discovery.set_current_game(user.id, app_id)
    stored = service.profiles.user(user.id)
    assert service.discovery.current_game(stored) == app_id
    assert any(game.app_id == app_id and game.minutes == 60 for game in stored.games)


def test_inflight_import_rejected_after_disconnect(service, users, steam_library):
    a = users(1)
    snapshot = steam_library(a.id, [{"appid": 730}])
    service.steam.disconnect_steam(a.id)
    with pytest.raises(DomainError):
        service.steam.save_public_steam(a.id, snapshot, expected_steam_id=snapshot.steam_id)


def test_reaction_limit_applies(service, users):
    a, b = users(1), users(2)
    service.discovery.feed(a.id)
    with service.db.session.begin() as session:
        for _ in range(120):
            session.add(Event(actor_id=a.id, target_id=b.id, kind="skip", details={}))
    with pytest.raises(DomainError, match="Лимит"):
        service.discovery.react(a.id, b.id, "like")
