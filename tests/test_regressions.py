import asyncio
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import select

from app.config import ROOT, Settings
from app.db import Database
from app.developer import DeveloperService
from app.main import create_app
from app.models import Event, ProfileSequence, TelegramBan, User, utcnow
from app.recommender import AffinityModel, prepare, score
from app.service import DomainError
from app.steam import SteamClient
from test_bot import Harness


def test_ban_survives_deletion_and_restart_until_explicit_unban(service, users):
    user = users(123)
    service.moderate(user.id, True)
    service.delete_user(user.telegram_id)
    service.db.initialize()
    assert service.user_by_telegram(123) is None
    assert service.is_banned(123)
    assert service.banned_accounts() == [123]
    with pytest.raises(DomainError, match="заблокирована"):
        users(123)
    service.moderate(123, False, telegram=True)
    assert not service.is_banned(123)
    assert users(123).banned is False


def test_legacy_bans_are_migrated_idempotently(service, users):
    user = users(123)
    with service.db.session.begin() as session:
        session.get(User, user.id).banned = True
    service.db.initialize()
    service.db.initialize()
    assert service.banned_accounts() == [123]
    service.moderate(user.id, False)
    service.db.initialize()
    assert not service.is_banned(123)


def test_developer_panel_uses_persistent_ban_policy(service, users):
    user = users(123)
    dev = DeveloperService(service, Settings(_env_file=None, developer_ids="1"))
    dev.update_profile(1, user.id, {"banned": True})
    assert service.is_banned(123)
    dev.update_profile(1, user.id, {"banned": False})
    assert not service.is_banned(123)
    with service.db.session() as session:
        assert session.get(TelegramBan, 123) is None


async def test_admin_can_unban_deleted_profile_but_regular_user_cannot(service, users):
    user = users(123)
    harness = Harness(service, admin_ids="1")
    try:
        await harness.update(1, f"/ban {user.id}")
        assert service.is_banned(123)
        service.delete_user(123)
        await harness.update(123, "/start")
        assert "заблокирована" in harness.sent()[-1].text
        await harness.update(999, "/unban tg:123")
        assert service.is_banned(123)
        await harness.update(1, "/bans")
        assert "123" in harness.sent()[-1].text
        await harness.update(1, "/unban tg:123")
        assert not service.is_banned(123)
        await harness.update(123, "/start")
        assert "соглас" in harness.sent()[-1].text
    finally:
        await harness.bot.session.close()


def test_deleted_profile_ids_are_never_reused_even_after_restart(service, users):
    viewer, deleted = users(1), users(2)
    service.feed(viewer.id)
    service.delete_user(deleted.telegram_id)
    service.db.initialize()
    replacement = users(3)
    assert replacement.id > deleted.id
    with pytest.raises(DomainError, match="недоступна"):
        service.react(viewer.id, deleted.id, "like")
    assert service.user(deleted.id) is None


def test_profile_sequence_bootstraps_above_legacy_ids(service, users):
    existing = users(1)
    with service.db.session.begin() as session:
        session.delete(session.get(ProfileSequence, 1))
    service.db.initialize()
    assert users(2).id > existing.id


@pytest.mark.parametrize("method", ["public", "openid"])
def test_switching_steam_clears_previous_account_import_and_cache(service, users, method):
    user = users(1)
    old, new = "76561198000000001", "76561198000000002"
    service.link_public_profile(user.id, old)
    service.import_games(user.id, [{"appid": 105600, "playtime_forever": 600},
                                   {"appid": 730, "playtime_forever": 1000}], expected_steam_id=old)
    if method == "openid":
        ticket = service.login_ticket(user.id)
        browser = service.begin_login(ticket)
        service.link_steam(ticket, browser, new)
    else:
        service.link_public_profile(user.id, new)
    changed = service.user(user.id)
    assert changed.steam_id == new
    assert changed.steam_verified is (method == "openid")
    assert changed.steam_checked_at is None and changed.steam_import_status == "not_loaded"
    assert {entry.app_id for entry in changed.games} == {105600, 548430}
    assert all(entry.selected and not entry.imported and entry.minutes == 0 for entry in changed.games)
    with pytest.raises(DomainError, match="Импорт отменён"):
        service.import_games(user.id, [{"appid": 730}], expected_steam_id=old)


def test_confirming_same_steam_keeps_existing_library(service, users):
    user = users(1)
    steam_id = "76561198000000001"
    service.link_public_profile(user.id, steam_id)
    service.import_games(user.id, [{"appid": 730, "playtime_forever": 600}], expected_steam_id=steam_id)
    checked = service.user(user.id).steam_checked_at
    ticket = service.login_ticket(user.id)
    browser = service.begin_login(ticket)
    service.link_steam(ticket, browser, steam_id)
    confirmed = service.user(user.id)
    assert confirmed.steam_verified and confirmed.steam_checked_at == checked
    assert any(entry.app_id == 730 and entry.imported for entry in confirmed.games)


def test_changing_public_steam_invalidates_pending_openid_ticket(service, users):
    user = users(1)
    ticket = service.login_ticket(user.id)
    browser = service.begin_login(ticket)
    service.link_public_profile(user.id, "76561198000000002")
    with pytest.raises(DomainError, match="Сессия Steam"):
        service.link_steam(ticket, browser, "76561198000000001")


def test_duplicate_imported_games_are_combined_before_database_write(service, users):
    user = users(1)
    count = service.import_games(user.id, [{"appid": 730, "playtime_forever": 600},
                                          {"appid": 730, "playtime_forever": 100, "playtime_2weeks": 60}])
    imported = [entry for entry in service.user(user.id).games if entry.app_id == 730]
    assert count == 1 and len(imported) == 1
    assert imported[0].minutes == 600 and imported[0].recent_minutes == 60


@pytest.mark.parametrize("games", [None, {}, [None], [{"appid": 0}], [{"appid": 730, "playtime_forever": -1}]])
def test_malformed_import_does_not_delete_previous_snapshot(service, users, games):
    user = users(1)
    service.import_games(user.id, [{"appid": 730, "playtime_forever": 600}])
    with pytest.raises(DomainError, match="Steam вернул"):
        service.import_games(user.id, games)
    assert any(entry.app_id == 730 and entry.minutes == 600 for entry in service.user(user.id).games)


@pytest.mark.parametrize("payload", [
    {"game_count": 2}, {"game_count": 1, "games": []},
    {"game_count": 0, "games": {}}, {"game_count": -1}, {"game_count": True}, None,
])
async def test_incomplete_steam_response_is_not_treated_as_empty_library(payload):
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda _: httpx.Response(200, json={"response": payload})
    )) as client:
        with pytest.raises(DomainError):
            await SteamClient("key", client).owned_games("76561198000000001")


@pytest.mark.parametrize("changes", [
    {"gender": "male"}, {"looking_for": "female"}, {"purpose": "friends"}, {"age_min": 30},
])
def test_mutual_filters_are_applied_before_candidate_limit(service, users, monkeypatch, changes):
    monkeypatch.setattr("app.service.CANDIDATE_LIMIT", 1)
    actor = users(1, gender="male", looking_for="female", purpose="romance")
    compatible = users(2, gender="female", looking_for="male", purpose="romance")
    incompatible = {"gender": "female", "looking_for": "male", "purpose": "romance", **changes}
    users(3, **incompatible)
    with service.db.session.begin() as session:
        session.get(User, compatible.id).last_active = utcnow() - timedelta(days=1)
    feed = service.feed(actor.id)
    assert [row["profile"]["id"] for row in feed] == [compatible.id]


def test_feed_serializes_only_selected_profiles_and_releases_lock(service, users, monkeypatch):
    actor = users(1)
    for number in range(2, 5):
        users(number)
    serialized = []
    original_profile = service.public_profile
    original_score = score

    def serialize(user):
        serialized.append(user.id)
        return original_profile(user)

    def rank(*args, **kwargs):
        def acquire_from_other_thread():
            acquired = service.lock.acquire(blocking=False)
            if acquired:
                service.lock.release()
            return acquired
        with ThreadPoolExecutor(max_workers=1) as executor:
            assert executor.submit(acquire_from_other_thread).result(timeout=2)
        return original_score(*args, **kwargs)

    monkeypatch.setattr(service, "public_profile", serialize)
    monkeypatch.setattr("app.service.score", rank)
    result = service.feed(actor.id, limit=1)
    assert serialized == [result[0]["profile"]["id"]]


@pytest.mark.parametrize("change", ["ban_actor", "ban_candidate", "delete_candidate"])
def test_feed_rechecks_availability_after_ranking(service, users, monkeypatch, change):
    actor, candidate = users(1), users(2)

    def rank(*args, **kwargs):
        if change == "ban_actor":
            service.moderate(actor.id, True)
        elif change == "ban_candidate":
            service.moderate(candidate.id, True)
        else:
            service.delete_user(candidate.telegram_id)
        return score(*args, **kwargs)

    monkeypatch.setattr("app.service.score", rank)
    assert service.feed(actor.id) == []
    with service.db.session() as session:
        assert list(session.scalars(select(Event))) == []


async def test_discovery_ranking_does_not_block_other_telegram_updates(service, users, monkeypatch):
    users(1)
    started, release = threading.Event(), threading.Event()
    original = service.feed

    def slow_feed(*args, **kwargs):
        started.set()
        assert release.wait(timeout=3)
        return original(*args, **kwargs)

    monkeypatch.setattr(service, "feed", slow_feed)
    harness = Harness(service)
    task = asyncio.create_task(harness.update(1, "/find"))
    try:
        assert await asyncio.to_thread(started.wait, 2)
        await asyncio.wait_for(harness.update(2, "/id"), timeout=1)
        assert not task.done()
    finally:
        release.set()
        await task
        await harness.bot.session.close()


def test_readonly_availability_does_not_score_load_profiles_or_record_events(service, users, monkeypatch):
    actor = users(1)
    users(2)
    monkeypatch.setattr("app.service.score", lambda *a, **k: pytest.fail("Availability must not rank"))
    assert service.feed(actor.id, availability_only=True, record_impressions=False)
    assert service.user(actor.id).last_active == actor.last_active
    with service.db.session() as session:
        assert list(session.scalars(select(Event))) == []


def test_sparse_model_matches_pairwise_reference_with_aliases_and_incoming_edges(tmp_path):
    path = tmp_path / "model.json"
    path.write_text(json.dumps({"neighbors": {"old a": {"b": 0.6}, "c": {"old a": 0.8}}}))
    model = AffinityModel(path)

    def profile(games):
        entries = [SimpleNamespace(app_id=index, selected=True, minutes=60 * index, recent_minutes=0,
                                   game=SimpleNamespace(name=name, aliases=aliases, genres=["coop"], coop=True))
                   for index, (name, aliases) in enumerate(games, 1)]
        return SimpleNamespace(games=entries, timezone="Europe/Moscow", start_hour=19, end_hour=23)

    a = profile([("New A", ["Old A"]), ("Unknown", [])])
    b = profile([("B", []), ("C", [])])
    now = datetime(2026, 11, 22, tzinfo=timezone.utc)
    left, right = prepare(a, now), prepare(b, now)
    for source, target in ((left, right), (right, left)):
        expected = sum(weight * max(model.game_similarity(source.games[i], target.games[j])
                                    for j in target.weights)
                       for i, weight in source.weights.items()) / sum(source.weights.values())
        assert model.library_similarity(source, target) == pytest.approx(expected)
    assert score(a, b, model, now) == score(a, b, model, now, prepared_a=left)


@pytest.mark.parametrize("hour,minute,ongoing", [(15, 0, False), (17, 15, True)])
def test_next_overlap_still_uses_viewer_timezone_with_cached_features(service, users, hour, minute, ongoing):
    a, b = users(1, timezone="Europe/Moscow", start_hour=19, end_hour=23), users(
        2, timezone="Europe/Moscow", start_hour=19, end_hour=23)
    window = score(a, b, service.model, datetime(2026, 11, 22, hour, minute, tzinfo=timezone.utc))["next_overlap"]
    assert window["end"] == "23:00" and window["ongoing"] is ongoing and window["day_offset"] == 0


@pytest.mark.parametrize("url", ["https://", "https://user:password@example.com", "https://example.com/?key=x",
                                 "https://example.com/#fragment", "ftp://example.com", "https://example.com:bad"])
def test_public_url_rejects_invalid_addresses_and_embedded_credentials(url):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, public_url=url)


def test_configurable_database_directory_does_not_move_bundled_assets(tmp_path):
    directory = tmp_path / "nested" / "storage"
    demo = Settings(_env_file=None, data_dir=directory)
    live = Settings(_env_file=None, data_dir=directory, bot_token="configured")
    assert demo.db_url.endswith("/nested/storage/demo.sqlite3")
    assert live.db_url.endswith("/nested/storage/bot.sqlite3")
    assert directory.is_dir() and (ROOT / "data/catalog.json").is_file()


def test_bundled_catalog_refreshes_metadata_without_removing_user_games(service, users, monkeypatch, tmp_path):
    user = users(1)
    service.import_games(user.id, [{"appid": 105600, "playtime_forever": 600}])
    (tmp_path / "data").mkdir()
    (tmp_path / "data/catalog.json").write_text(json.dumps([
        {"app_id": 105600, "name": "Updated Terraria", "genres": ["sandbox"], "aliases": ["Terraria"], "coop": True}
    ]))
    monkeypatch.setattr("app.db.ROOT", tmp_path)
    service.db.initialize()
    entries = {entry.app_id: entry for entry in service.user(user.id).games}
    assert set(entries) == {105600, 548430}
    assert entries[105600].selected and entries[105600].minutes == 600
    assert entries[105600].game.name == "Updated Terraria" and entries[105600].game.genres == ["sandbox"]


def test_health_endpoint_returns_503_when_live_poller_is_not_running(tmp_path):
    settings = Settings(_env_file=None, bot_token="configured")
    database = Database("sqlite:///" + (tmp_path / "health.sqlite3").as_posix())
    app = create_app(settings, database)
    client = TestClient(app)
    try:
        # Without the context manager the lifespan does not connect to Telegram.
        response = client.get("/health")
        assert response.status_code == 503 and response.json()["status"] == "degraded"
        app.state.polling = SimpleNamespace(done=lambda: False)
        assert client.get("/health").status_code == 200
        app.state.polling = SimpleNamespace(done=lambda: True)
        assert client.get("/health").status_code == 503
    finally:
        client.close()
        database.engine.dispose()
