from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest
from sqlalchemy import func, select

from app.models import Event, FSMRecord, LoginTicket, Match, Reaction, User, utcnow
from app.schemas import ProfileInput
from app.service import DomainError


def mutual(service, a, b):
    service.feed(a.id)
    service.feed(b.id)
    service.react(a.id, b.id, "like")
    return service.react(b.id, a.id, "like")


def test_mutual_like_is_idempotent_under_concurrency(service, users):
    a, b = users(1), users(2)
    result = mutual(service, a, b)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: service.react(a.id, b.id, "like"), range(8)))
    assert result["matched"] and all(row["matched"] and not row["new"] for row in results)
    assert len(service.matches(a.id)) == 1
    assert not service.feed(a.id)


def test_cannot_like_arbitrary_unseen_id(service, users):
    a, b = users(1), users(2)
    with pytest.raises(DomainError):
        service.react(a.id, b.id, "like")


def test_blocks_apply_in_both_directions_and_stop_messages(service, users):
    a, b = users(1), users(2)
    result = mutual(service, a, b)
    service.react(a.id, b.id, "block")
    assert not service.matches(a.id) and not service.matches(b.id)
    with pytest.raises(DomainError):
        service.match_peer(b.id, result["match_id"])


def test_report_also_blocks(service, users):
    a, b = users(1), users(2)
    service.feed(a.id)
    service.react(a.id, b.id, "report", "Спам")
    assert not service.feed(b.id)


def test_pause_and_inactive_profiles_are_hidden(service, users):
    a, b = users(1), users(2)
    service.toggle_active(b.id)
    assert not service.feed(a.id)
    service.toggle_active(b.id)
    with service.db.session.begin() as session:
        session.get(User, b.id).last_active = utcnow() - timedelta(days=31)
    assert not service.feed(a.id)


def test_demo_users_never_appear_for_live_users(service, users):
    a = users(1)
    service.save_profile(-1, ProfileInput(name="Демо", age=25, game_ids=[105600]), demo=True)
    assert service.feed(a.id) == []


def test_account_deletion_cascades_and_removes_drafts(service, users):
    a, b = users(1), users(2)
    mutual(service, a, b)
    ticket = service.login_ticket(a.id)
    with service.db.session.begin() as session:
        session.add(FSMRecord(key="test", telegram_id=1, state="draft", data={"name":"test"}))
    service.delete_user(a.telegram_id)
    assert service.user(a.id) is None
    assert not service.matches(b.id)
    with service.db.session() as session:
        for model in [Event, Match, Reaction, LoginTicket, FSMRecord]:
            assert session.scalar(select(func.count()).select_from(model)) == 0
    with pytest.raises(DomainError):
        service.begin_login(ticket)


def test_import_preserves_manual_choices_and_disconnect_removes_import(service, users):
    a = users(1)
    service.import_games(a.id, [{"appid":105600,"name":"Terraria","playtime_forever":600},
                                {"appid":9999991,"name":"New Game","playtime_forever":500}])
    imported = service.user(a.id)
    assert len(imported.games) == 3
    assert next(g for g in imported.games if g.app_id == 105600).selected
    service.disconnect_steam(a.id)
    after = service.user(a.id)
    assert {entry.app_id for entry in after.games} == {105600,548430}
    assert all(entry.minutes == 0 and not entry.imported for entry in after.games)


def test_inflight_import_rejected_after_disconnect(service, users):
    a = users(1)
    service.disconnect_steam(a.id)
    with pytest.raises(DomainError):
        service.import_games(a.id, [{"appid":730}], expected_steam_id="76561198000000001")


def test_tickets_expire_are_browser_bound_and_single_use(service, users):
    a = users(1)
    ticket = service.login_ticket(a.id)
    browser = service.begin_login(ticket)
    with pytest.raises(DomainError):
        service.verify_ticket(ticket, "wrong browser")
    assert service.verify_ticket(ticket, browser) == a.id
    assert service.link_steam(ticket, browser, "76561198000000001") == a.id
    with pytest.raises(DomainError):
        service.link_steam(ticket, browser, "76561198000000001")
    second = service.login_ticket(a.id)
    with service.db.session.begin() as session:
        for row in session.scalars(select(LoginTicket)):
            row.expires_at = utcnow() - timedelta(seconds=1)
    with pytest.raises(DomainError):
        service.begin_login(second)


def test_steam_account_cannot_be_claimed_twice(service, users):
    a, b = users(1), users(2)
    ticket = service.login_ticket(a.id)
    browser = service.begin_login(ticket)
    service.link_steam(ticket, browser, "76561198000000001")
    ticket = service.login_ticket(b.id)
    browser = service.begin_login(ticket)
    with pytest.raises(DomainError):
        service.link_steam(ticket, browser, "76561198000000001")


def test_reaction_limit_applies(service, users):
    a, b = users(1), users(2)
    service.feed(a.id)
    with service.db.session.begin() as session:
        for _ in range(120):
            session.add(Event(actor_id=a.id, target_id=b.id, kind="skip", details={}))
    with pytest.raises(DomainError, match="Лимит"):
        service.react(a.id, b.id, "like")
