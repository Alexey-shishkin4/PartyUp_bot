import pytest
from sqlalchemy import func,select

from app.bot import profile_text
from app.models import Event,Match,Reaction,User
from app.schemas import ProfileInput
from app.service import DomainError
from app.test_profiles import fixture_definitions,fixture_photo,seed_test_profiles


def test_fixtures_with_photos_and_source_links(service):
    seed_test_profiles(service)
    with service.db.session() as session:
        records = list(session.scalars(select(User).where(User.is_test.is_(True))))
    assert len(records) == len(fixture_definitions()) == 5
    assert {record.name for record in records} == {"Мира","Ника","Ася","Лера","Соня"}
    for user in records:
        assert user.telegram_id < 0 and user.age >= 18 and not user.is_demo
        assert len(user.games) == 3 and fixture_photo(user.telegram_id).is_file()
        info = service.public_profile(user)
        assert info["is_test"] and info["has_photo"] and "pinterest.com/" in info["photo_source_url"]
        assert "ТЕСТОВАЯ АНКЕТА" in profile_text(info)


def test_seed_is_idempotent_and_preserves_real_profile(service,users):
    real = users(1)
    service.set_photo(real.id,"real-photo")
    seed_test_profiles(service)
    seed_test_profiles(service)
    with service.db.session() as session:
        assert session.scalar(select(func.count()).select_from(User)) == len(fixture_definitions())+1
    unchanged = service.user(real.id)
    assert unchanged.name == real.name and unchanged.photo_file_id == "real-photo" and not unchanged.is_test


def test_search_respects_filters_while_test_gallery_can_preview_all(service,users):
    real = users(1,age=99,looking_for="male",age_min=90,age_max=99)
    seed_test_profiles(service)
    assert service.feed(real.id) == []
    rows = service.feed(real.id,test_only=True)
    assert len(rows) == len(fixture_definitions()) and all(row["profile"]["is_test"] for row in rows)
    with service.db.session() as session:
        assert set(session.scalars(select(Event.kind))) == {"test_impression"}


def test_regular_search_contains_real_and_labelled_static_test_profiles(service,users):
    viewer = users(1)
    real_peer = users(2)
    seed_test_profiles(service)
    rows = service.feed(viewer.id)
    assert len(rows) == 6
    assert any(row["profile"]["id"] == real_peer.id and not row["profile"]["is_test"] for row in rows)
    assert len([row for row in rows if row["profile"]["is_test"]]) == 5
    with service.db.session() as session:
        assert session.scalar(select(func.count()).select_from(Event).where(Event.kind == "test_impression")) == 5
        assert session.scalar(select(func.count()).select_from(Event).where(Event.kind == "impression")) == 1


def test_regular_search_test_like_is_recorded_without_creating_match(service,users):
    viewer = users(1)
    seed_test_profiles(service)
    target = service.feed(viewer.id)[0]["profile"]["id"]
    result = service.react(viewer.id,target,"like")
    assert result["test_profile"] and not result["matched"]
    assert len(service.feed(viewer.id)) == 4
    assert service.matches(viewer.id) == []
    with service.db.session() as session:
        assert set(session.scalars(select(Event.kind))) == {"test_impression","test_like"}


def test_test_like_cannot_match_or_contaminate_real_training_events(service,users):
    real = users(1)
    seed_test_profiles(service)
    target = service.feed(real.id,test_only=True)[0]["profile"]["id"]
    # Even if a stale synthetic reverse-like exists, no match may be created.
    with service.db.session.begin() as session:
        session.add(Reaction(actor_id=target,target_id=real.id,kind="like"))
    result = service.react(real.id,target,"like")
    assert result == {"matched":False,"new":False,"test_profile":True}
    assert service.matches(real.id) == []
    with service.db.session() as session:
        assert session.scalar(select(func.count()).select_from(Match)) == 0
        assert "like" not in set(session.scalars(select(Event.kind)))
    assert len(service.feed(real.id,test_only=True)) == len(fixture_definitions())-1
    service.reset_test_reactions(real.id)
    assert len(service.feed(real.id,test_only=True)) == len(fixture_definitions())


def test_fixture_cannot_be_used_as_authenticated_actor(service,users):
    real = users(1)
    seed_test_profiles(service)
    fixture = service.user_by_telegram(next(iter(fixture_definitions())))
    assert service.feed(fixture.id) == []
    with pytest.raises(DomainError):
        service.react(fixture.id,real.id,"like")
    with pytest.raises(DomainError):
        service.save_profile(fixture.telegram_id,ProfileInput(name="Changed",age=25,game_ids=[105600]))


def test_banned_fixture_does_not_prevent_startup(service):
    seed_test_profiles(service)
    telegram = next(iter(fixture_definitions()))
    user = service.user_by_telegram(telegram)
    with service.db.session.begin() as session:
        session.get(User,user.id).banned = True
    seed_test_profiles(service)
    assert service.user(user.id).banned


def test_changed_test_photo_cache_cleared_and_removed_fixture_retired(service,users):
    real = users(1)
    service.set_photo(real.id,"real-photo")
    seed_test_profiles(service)
    current = service.user_by_telegram(next(iter(fixture_definitions())))
    service.set_photo(current.id,"old-telegram-cached-photo")
    retired_id = service.save_profile(-10999,ProfileInput(name="Старый пример",age=25,game_ids=[105600]),
                                      test_profile=True,photo_file_id="obsolete-photo")
    seed_test_profiles(service)
    assert service.photo_file(current.id) is None
    assert not service.user(retired_id).active and service.photo_file(retired_id) is None
    assert service.photo_file(real.id) == "real-photo"
    assert len(service.feed(real.id,test_only=True)) == 5
