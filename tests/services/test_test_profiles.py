import pytest
from sqlalchemy import func, select

from app.domain.errors import DomainError
from app.domain.profiles import ProfileInput
from app.domain.steam import PublicProfile
from app.persistence.models import Event, Match, Reaction, User
from app.services.fixture_catalog import fixture_definitions, fixture_photo
from app.services.fixtures import seed_test_profiles


def test_fixtures_with_photos(service):
    seed_test_profiles(service)
    fixtures = fixture_definitions()
    with service.db.session() as session:
        records = list(session.scalars(select(User).where(User.is_test.is_(True))))
    assert len(records) == len(fixtures) == 14
    for user in records:
        row = fixtures[user.telegram_id]
        assert user.telegram_id < 0 and user.age >= 18 and not user.steam_id
        assert user.name == row["name"] and fixture_photo(user.telegram_id).is_file()
        assert service.profiles.test_photo_file(user.id) == fixture_photo(user.telegram_id)
        if "library" in row:
            assert {entry.app_id: (entry.minutes, entry.recent_minutes, entry.imported) for entry in user.games} == {
                game["app_id"]: (game["hours"] * 60, game.get("recent_hours", 0) * 60, True)
                for game in row["library"]
            }
            assert not any(entry.selected for entry in user.games)
        else:
            assert {entry.app_id for entry in user.games} == set(row["game_ids"])


def test_seed_is_idempotent_and_preserves_real_profile(service,users):
    real = users(1)
    service.profiles.set_photo(real.id,"real-photo")
    seed_test_profiles(service)
    seed_test_profiles(service)
    with service.db.session() as session:
        assert session.scalar(select(func.count()).select_from(User)) == len(fixture_definitions())+1
    unchanged = service.profiles.user(real.id)
    assert unchanged.name == real.name and unchanged.photo_file_id == "real-photo" and not unchanged.is_test


def test_search_respects_filters_while_test_gallery_can_preview_all(service,users):
    real = users(1,age=99,looking_for="male",age_min=90,age_max=99)
    seed_test_profiles(service)
    assert service.discovery.feed(real.id) == []
    rows = service.discovery.feed(real.id, limit=len(fixture_definitions()), test_only=True)
    assert len(rows) == len(fixture_definitions()) and all(row["profile"]["is_test"] for row in rows)
    with service.db.session() as session:
        assert set(session.scalars(select(Event.kind))) == {"test_impression"}


def test_regular_search_contains_real_and_static_test_profiles(service,users):
    viewer = users(1)
    real_peer = users(2)
    seed_test_profiles(service)
    total = len(fixture_definitions())
    rows = service.discovery.feed(viewer.id, limit=total + 1)
    assert len(rows) == total + 1
    assert any(row["profile"]["id"] == real_peer.id and not row["profile"]["is_test"] for row in rows)
    assert len([row for row in rows if row["profile"]["is_test"]]) == total
    with service.db.session() as session:
        assert session.scalar(select(func.count()).select_from(Event).where(Event.kind == "test_impression")) == total
        assert session.scalar(select(func.count()).select_from(Event).where(Event.kind == "impression")) == 1


def test_regular_search_test_like_is_recorded_without_creating_match(service,users):
    viewer = users(1)
    seed_test_profiles(service)
    target = service.discovery.feed(viewer.id)[0]["profile"]["id"]
    result = service.discovery.react(viewer.id,target,"like")
    assert result["test_profile"] and not result["matched"]
    assert len(service.discovery.feed(viewer.id, limit=len(fixture_definitions()))) == len(fixture_definitions()) - 1
    assert service.discovery.matches(viewer.id) == []
    with service.db.session() as session:
        assert set(session.scalars(select(Event.kind))) == {"test_impression","test_like"}


def test_test_like_cannot_match_or_contaminate_real_training_events(service,users):
    real = users(1)
    seed_test_profiles(service)
    target = service.discovery.feed(real.id,test_only=True)[0]["profile"]["id"]
    # Even if a stale synthetic reverse-like exists, no match may be created.
    with service.db.session.begin() as session:
        session.add(Reaction(actor_id=target,target_id=real.id,kind="like"))
    result = service.discovery.react(real.id,target,"like")
    assert result == {"matched":False,"new":False,"test_profile":True}
    assert service.discovery.matches(real.id) == []
    with service.db.session() as session:
        assert session.scalar(select(func.count()).select_from(Match)) == 0
        assert "like" not in set(session.scalars(select(Event.kind)))
    total = len(fixture_definitions())
    assert len(service.discovery.feed(real.id, limit=total, test_only=True)) == total - 1
    service.discovery.reset_test_reactions(real.id)
    assert len(service.discovery.feed(real.id, limit=total, test_only=True)) == total


def test_fixture_cannot_be_used_as_authenticated_actor(service,users):
    real = users(1)
    seed_test_profiles(service)
    fixture = service.profiles.user_by_telegram(next(iter(fixture_definitions())))
    assert service.discovery.feed(fixture.id) == []
    with pytest.raises(DomainError):
        service.discovery.react(fixture.id,real.id,"like")
    with pytest.raises(DomainError):
        service.profiles.save_profile(fixture.telegram_id,ProfileInput(name="Changed",age=25,game_ids=[105600]))


def test_banned_fixture_does_not_prevent_startup(service):
    seed_test_profiles(service)
    telegram = next(iter(fixture_definitions()))
    user = service.profiles.user_by_telegram(telegram)
    with service.db.session.begin() as session:
        session.get(User,user.id).banned = True
    seed_test_profiles(service)
    assert service.profiles.user(user.id).banned


def test_changed_test_photo_cache_cleared_and_removed_fixture_retired(service,users):
    real = users(1)
    service.profiles.set_photo(real.id,"real-photo")
    seed_test_profiles(service)
    current = service.profiles.user_by_telegram(next(iter(fixture_definitions())))
    service.profiles.set_photo(current.id,"old-telegram-cached-photo")
    retired_id = service.profiles.save_profile(-10999,ProfileInput(name="Старый пример",age=25,game_ids=[105600]),
                                      test_profile=True,photo_file_id="obsolete-photo")
    seed_test_profiles(service)
    assert service.profiles.photo_file(current.id) is None
    assert not service.profiles.user(retired_id).active and service.profiles.photo_file(retired_id) is None
    assert service.profiles.photo_file(real.id) == "real-photo"
    assert len(service.discovery.feed(real.id, limit=len(fixture_definitions()), test_only=True)) == len(fixture_definitions())


def test_fixture_libraries_rank_different_matches_for_two_reference_profiles(service, users):
    seed_test_profiles(service)

    def discover(telegram_id, steam_id, url, library):
        viewer = users(telegram_id, game_ids=[next(iter(library))])
        games = [{"appid": app_id, "playtime_forever": hours * 60} for app_id, hours in library.items()]
        service.steam.save_public_steam(viewer.id, PublicProfile(steam_id, url, games=games, coverage="library"))
        rows = service.discovery.feed(viewer.id, limit=len(fixture_definitions()), test_only=True)
        return {row["profile"]["name"]: row for row in rows}

    shooters = discover(1, "76561198217144273", "https://steamcommunity.com/profiles/76561198217144273",
                        {730: 1700, 1172470: 45, 218620: 55, 1966720: 55, 322330: 70})
    assert next(iter(shooters)) == "Лина"
    assert shooters["Лина"]["common_games"][0] == "Counter-Strike 2"
    assert shooters["Дядя Кефир"]["common_games"] == []
    assert shooters["Дядя Кефир"]["components"]["genres"] > 0

    dota = discover(2, "76561198303545401", "https://steamcommunity.com/id/zxckingg12345",
                    {570: 3500, 730: 730, 271590: 700, 105600: 290, 1422450: 270, 4000: 250,
                     1245620: 230, 322330: 90, 2622380: 175, 431960: 60, 1281930: 75,
                     304930: 70, 286160: 390, 427520: 480, 1621690: 300})
    assert next(iter(dota)) == "Рина"
    assert dota["Рина"]["common_games"][0] == "Dota 2"
    assert "Dota 2" not in dota["Рина"]["profile"]["games"]
    assert dota["Вика"]["score"] > dota["Марк"]["score"] > dota["Кай"]["score"]
