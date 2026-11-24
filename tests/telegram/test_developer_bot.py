import pytest
from aiogram.methods import AnswerCallbackQuery
from aiogram.types import FSInputFile
from sqlalchemy import func, select

from app.persistence.models import DeveloperAudit, Event, User
from app.services.fixture_catalog import fixture_definitions
from app.services.fixtures import seed_test_profiles

from .harness import Harness, photo_sizes


async def test_unapproved_user_cannot_view_or_edit_from_command_or_callback(service, users):
    target = users(2)
    harness = Harness(service, developer_ids="1")
    await harness.update(2, "/dev")
    assert "только разрешённым" in harness.sent()[-1].text
    await harness.update(2, callback=f"dev:profile:{target.id}")
    await harness.update(2, callback=f"dev:toggle:banned:{target.id}")
    assert not service.profiles.user(target.id).banned and not harness.photos_sent()
    with service.db.session() as session:
        assert session.scalar(select(func.count()).select_from(DeveloperAudit)) == 0
    await harness.bot.session.close()


async def test_developer_can_list_and_edit_another_profile(service, users):
    target = users(2)
    harness = Harness(service, developer_ids="1")
    await harness.update(1, "/dev")
    assert "Режим разработчика" in harness.sent()[-1].text
    await harness.update(1, callback="dev:list:all:0")
    assert "Игрок 2" in str(harness.sent()[-1].reply_markup)
    await harness.update(1, callback=f"dev:profile:{target.id}")
    assert "Telegram ID" in harness.sent()[-1].text
    await harness.update(1, callback=f"dev:field:{target.id}:name")
    await harness.update(1, "Имя от разработчика")
    assert service.profiles.user(target.id).name == "Имя от разработчика"
    assert service.profiles.user_by_telegram(1) is None
    await harness.bot.session.close()


async def test_developer_can_edit_photo_games_and_enum_fields(service, users):
    target = users(2)
    harness = Harness(service, developer_ids="1")
    await harness.update(1, callback=f"dev:photo:{target.id}")
    await harness.update(1, photos=photo_sizes("admin-photo"))
    assert service.profiles.photo_file(target.id) == "admin-photo"
    assert harness.photos_sent()[-1].photo == "admin-photo"
    await harness.update(1, callback=f"dev:games:{target.id}")
    await harness.update(1, callback=f"dev:game:{target.id}:570")
    await harness.update(1, callback=f"dev:games_save:{target.id}")
    assert any(entry.app_id == 570 and entry.selected for entry in service.profiles.user(target.id).games)
    await harness.update(1, callback=f"dev:field:{target.id}:purpose")
    await harness.update(1, callback=f"dev:pick:{target.id}:purpose:friends")
    assert service.profiles.user(target.id).purpose == "friends"
    await harness.update(1, callback=f"dev:photo:{target.id}")
    await harness.update(1, callback=f"dev:photo_remove:{target.id}")
    assert service.profiles.photo_file(target.id) is None
    await harness.bot.session.close()


async def test_revoked_permission_cannot_use_saved_edit_state(service, users):
    target = users(2)
    harness = Harness(service, developer_ids="1")
    await harness.update(1, callback=f"dev:field:{target.id}:name")
    harness.settings.developer_ids = ""
    await harness.update(1, "Не должно сохраниться")
    assert service.profiles.user(target.id).name == target.name
    assert "только разрешённым" in harness.sent()[-1].text
    await harness.bot.session.close()


async def test_developer_tools_do_not_message_other_profiles_or_record_fake_views(service, users):
    source = users(2)
    users(3)
    harness = Harness(service, developer_ids="1")
    await harness.update(1, callback=f"dev:preview:{source.id}")
    assert "Предпросмотр" in harness.sent()[-1].text
    assert all(message.chat_id == 1 for message in harness.sent())
    with service.db.session() as session:
        assert session.scalar(select(func.count()).select_from(Event)) == 0
    await harness.bot.session.close()


async def test_stale_game_button_does_not_edit_another_target(service, users):
    a, b = users(2), users(3)
    harness = Harness(service, developer_ids="1")
    await harness.update(1, callback=f"dev:games:{a.id}")
    await harness.update(1, callback=f"dev:game:{b.id}:570")
    assert "Кнопка устарела" in harness.sent()[-1].text
    assert not any(entry.app_id == 570 for entry in service.profiles.user(b.id).games)
    await harness.bot.session.close()


async def test_id_is_available_without_registration_and_cancel_stops_edit(service, users):
    target = users(2)
    harness = Harness(service, developer_ids="1")
    await harness.update(9, "/id", username="tester")
    assert "<code>9</code>" in harness.sent()[-1].text and "@tester" in harness.sent()[-1].text
    await harness.update(1, callback=f"dev:field:{target.id}:name")
    await harness.update(1, "/cancel")
    assert "Режим разработчика" in harness.sent()[-1].text
    assert service.profiles.user(target.id).name == target.name
    await harness.bot.session.close()


@pytest.mark.parametrize("field,value", [("age", "17"), ("age_range", "35-20"), ("timezone", "Mars/Test")])
async def test_invalid_edit_keeps_target_unchanged_and_allows_retry(service, users, field, value):
    target = users(2)
    harness = Harness(service, developer_ids="1")
    await harness.update(1, callback=f"dev:field:{target.id}:{field}")
    await harness.update(1, value)
    assert service.profiles.user(target.id).age == target.age
    assert "Проверь значение" in harness.sent()[-1].text
    await harness.bot.session.close()


async def test_developer_photo_state_still_checks_access(service, users):
    target = users(2)
    harness = Harness(service, developer_ids="1")
    await harness.update(1, callback=f"dev:photo:{target.id}")
    harness.settings.developer_ids = ""
    await harness.update(1, photos=photo_sizes("forbidden-photo"))
    assert service.profiles.photo_file(target.id) is None
    await harness.bot.session.close()


async def test_developer_commands_do_not_reveal_profiles_in_group_chat(service, users):
    from datetime import datetime, timezone

    from aiogram.types import Chat, Message, Update
    from aiogram.types import User as TelegramUser

    users(2)
    harness = Harness(service, developer_ids="1")
    update = Update(
        update_id=200,
        message=Message(
            message_id=1,
            date=datetime.now(timezone.utc),
            chat=Chat(id=-100, type="group", title="Group"),
            from_user=TelegramUser(id=1, is_bot=False, first_name="Developer"),
            text="/dev",
        ),
    )
    await harness.dp.feed_update(harness.bot, update)
    assert harness.sent() == [] and harness.photos_sent() == []
    await harness.bot.session.close()


async def test_developer_input_survives_restart(service, users):
    target = users(2)
    first = Harness(service, developer_ids="1")
    await first.update(1, callback=f"dev:field:{target.id}:bio")
    restarted = Harness(service, developer_ids="1")
    await restarted.update(1, "Описание после перезапуска")
    assert service.profiles.user(target.id).bio == "Описание после перезапуска"
    await first.bot.session.close()
    await restarted.bot.session.close()


async def test_banned_test_profile_can_be_viewed_with_photo(service):
    from app.persistence.models import User

    seed_test_profiles(service)
    target = service.profiles.user_by_telegram(next(iter(fixture_definitions())))
    with service.db.session.begin() as session:
        session.get(User, target.id).banned = True
    harness = Harness(service, developer_ids="1")
    await harness.update(1, f"/dev {target.id}")
    assert harness.photos_sent()
    assert (
        any("заблокирована" in (message.text or "") for message in harness.sent())
        or "заблокирована" in harness.photos_sent()[-1].caption
    )
    await harness.bot.session.close()


@pytest.mark.parametrize("long_bio", [False, True])
async def test_private_card_preview_uses_same_profile_text_as_discovery(service, users, long_bio):
    viewer = users(1)
    seed_test_profiles(service)
    target_id = service.discovery.feed(viewer.id, limit=1, record_impressions=False)[0]["profile"]["id"]
    if long_bio:
        with service.db.session.begin() as session:
            session.get(User, target_id).bio = "🙂" * 499
    target = service.profiles.user(target_id)
    harness = Harness(service, developer_ids="1")

    await harness.update(1, callback=f"dev:presentation:{target_id}")
    context = harness.sent()[0].text
    assert "Закрытый предпросмотр" in context and "вымышлены" in context
    photo = harness.photos_sent()[-1]
    assert isinstance(photo.photo, FSInputFile)
    card_text = photo.caption + "\n" + "\n".join(message.text for message in harness.sent()[1:])
    assert target.name in card_text
    assert "ДЕМО-ПРОФИЛЬ БОТА" not in card_text and "Источник фото" not in card_text
    assert "Telegram ID" not in card_text
    action_message = harness.sent()[-1] if long_bio else photo
    assert f"dev:profile:{target_id}" in str(action_message.reply_markup)
    if long_bio:
        assert target.bio in card_text and len(harness.sent()) == 2
    assert service.profiles.user(target_id).last_active == target.last_active
    assert service.profiles.user(target_id).is_test
    assert all(message.chat_id == 1 for message in [*harness.sent(), *harness.photos_sent()])
    with service.db.session() as session:
        assert session.scalar(select(func.count()).select_from(Event)) == 0
        assert session.scalar(select(func.count()).select_from(DeveloperAudit)) == 0

    sent_before_find = len(harness.sent())
    await harness.update(1, "/find")
    public_text = harness.photos_sent()[-1].caption + "\n" + "\n".join(
        message.text for message in harness.sent()[sent_before_find:]
    )
    assert target.name in public_text and target.bio in public_text
    assert "ДЕМО-ПРОФИЛЬ БОТА" not in public_text and "Источник фото" not in public_text
    await harness.bot.session.close()


@pytest.mark.parametrize("revoked", [False, True])
async def test_private_card_preview_denies_unapproved_or_revoked_developer(service, users, revoked):
    target = users(2)
    service.profiles.set_photo(target.id, "private-photo")
    harness = Harness(service, developer_ids="1" if revoked else "9")
    if revoked:
        harness.settings.developer_ids = ""
    await harness.update(1, callback=f"dev:presentation:{target.id}")
    assert not harness.sent() and not harness.photos_sent()
    response = next(call for call in harness.session.calls if isinstance(call, AnswerCallbackQuery))
    assert response.show_alert and "только разрешённым" in response.text
    await harness.bot.session.close()
