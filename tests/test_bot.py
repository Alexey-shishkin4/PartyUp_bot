from datetime import datetime, timezone

from aiogram.client.session.base import BaseSession
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import AnswerCallbackQuery, DeleteMessage, EditMessageMedia, EditMessageReplyMarkup, EditMessageText, GetChat, SendMessage, SendPhoto
from aiogram.types import CallbackQuery, Chat, ChatFullInfo, Document, FSInputFile, Message, PhotoSize, Update, User as TelegramUser
from sqlalchemy import func, select

from app.bot import build_bot, deliver_profile_alerts
from app.config import Settings
from app.models import FSMRecord


class FakeSession(BaseSession):
    def __init__(self):
        super().__init__()
        self.calls = []
        self.fail_photos = False
        self.usernames = {}

    async def close(self):
        pass

    async def make_request(self, bot, method, timeout=None):
        self.calls.append(method)
        if isinstance(method, SendMessage):
            return Message(message_id=len(self.calls), date=datetime.now(timezone.utc),
                           chat=Chat(id=int(method.chat_id), type="private"), text=method.text,
                           from_user=TelegramUser(id=bot.id, is_bot=True, first_name="PartyUp"))
        if isinstance(method, SendPhoto):
            if self.fail_photos:
                raise TelegramBadRequest(method=method,message="wrong file identifier")
            file_id = "uploaded-test-photo" if isinstance(method.photo,FSInputFile) else str(method.photo)
            return Message(message_id=len(self.calls), date=datetime.now(timezone.utc),
                           chat=Chat(id=int(method.chat_id), type="private"), caption=method.caption,
                           photo=[PhotoSize(file_id=file_id, file_unique_id="test-unique",
                                            width=1280, height=960)],
                           from_user=TelegramUser(id=bot.id, is_bot=True, first_name="PartyUp"))
        if isinstance(method, GetChat):
            return ChatFullInfo(id=int(method.chat_id), type="private", username=self.usernames.get(method.chat_id),
                                accent_color_id=0, max_reaction_count=0, accepted_gift_types={
                                    "unlimited_gifts": False, "limited_gifts": False,
                                    "unique_gifts": False, "premium_subscription": False,
                                    "gifts_from_channels": False,
                                })
        if isinstance(method, EditMessageMedia) and self.fail_photos:
            raise TelegramBadRequest(method=method, message="wrong file identifier")
        if isinstance(method, (AnswerCallbackQuery, DeleteMessage, EditMessageMedia, EditMessageReplyMarkup, EditMessageText)):
            return True
        raise AssertionError(f"Unexpected network operation: {type(method).__name__}")

    async def stream_content(self, url, headers=None, timeout=30, chunk_size=65536, raise_for_status=True):
        yield b""


class Harness:
    def __init__(self, service, public_steam=None, developer_ids="", admin_ids=""):
        settings = Settings(_env_file=None, bot_token="123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijk",
                            public_url="https://party.example",developer_ids=developer_ids,admin_ids=admin_ids)
        self.settings=settings
        self.bot, self.dp = build_bot(service, settings, None, public_steam)
        self.session = FakeSession()
        self.bot.session = self.session
        self.counter = 0

    async def update(self, user_id, text=None, callback=None, username=None, photos=None, document=None,
                     card_text=None, card_photo=False, card_id=None):
        self.counter += 1
        sender = TelegramUser(id=user_id, is_bot=False, first_name="Игрок", username=username)
        message = Message(message_id=card_id or self.counter, date=datetime.now(timezone.utc),
                          chat=Chat(id=user_id, type="private"), from_user=sender, text=text,
                          photo=photos, document=document)
        if callback:
            message = message.model_copy(update={
                "from_user": TelegramUser(id=self.bot.id, is_bot=True, first_name="PartyUp"),
                "text": card_text, "photo": photo_sizes() if card_photo else None,
            })
            update = Update(update_id=self.counter, callback_query=CallbackQuery(
                id=str(self.counter), from_user=sender, chat_instance="test", message=message, data=callback))
        else:
            update = Update(update_id=self.counter, message=message)
        return await self.dp.feed_update(self.bot, update)

    def sent(self):
        return [call for call in self.session.calls if isinstance(call, SendMessage)]

    def photos_sent(self):
        return [call for call in self.session.calls if isinstance(call, SendPhoto)]


def photo_sizes(file_id="photo-original", file_size=200000):
    return [PhotoSize(file_id="photo-small", file_unique_id="small",width=320,height=240,file_size=1000),
            PhotoSize(file_id=file_id,file_unique_id="large",width=1280,height=960,file_size=file_size)]


async def register(harness, telegram=1, with_photo=False):
    await harness.update(telegram, "/start")
    await harness.update(telegram, callback="consent:yes")
    await harness.update(telegram, "Алекс")
    await harness.update(telegram, "17")
    assert "18 до 99" in harness.sent()[-1].text
    await harness.update(telegram, "25")
    await harness.update(telegram, callback="form:purpose:both")
    await harness.update(telegram, callback="form:timezone:Asia/Novosibirsk")
    await harness.update(telegram, "22-2")
    await harness.update(telegram, callback="game:105600")
    await harness.update(telegram, callback="game:save")
    assert "Опубликовать" in str(harness.sent()[-1].reply_markup)
    await harness.update(telegram, callback="review:bio")
    await harness.update(telegram, "За спокойный кооп <и> разговоры")
    if with_photo:
        await harness.update(telegram, callback="review:photo")
        await harness.update(telegram, photos=photo_sizes())
    await harness.update(telegram, callback="review:publish", username="alex")


async def test_full_registration_and_profile(service):
    harness = Harness(service)
    await register(harness)
    user = service.user_by_telegram(1)
    assert user and user.name == "Алекс" and user.purpose == "both"
    assert user.timezone == "Asia/Novosibirsk"
    assert user.start_hour == 22 and user.end_hour == 2
    await harness.update(1, "/profile")
    assert "&lt;и&gt;" in harness.sent()[-1].text
    assert "Terraria" in harness.sent()[-1].text
    await harness.bot.session.close()


async def test_registration_survives_restart(service):
    first = Harness(service)
    await first.update(1, "/start")
    await first.update(1, callback="consent:yes")
    await first.update(1, "Новый игрок")
    second = Harness(service)
    await second.update(1, "26")
    assert "Чего ждёшь" in second.sent()[-1].text
    await first.bot.session.close()
    await second.bot.session.close()


async def test_match_shows_current_usernames_only_after_mutual_likes(service, users):
    a, b = users(1), users(2)
    service.feed(a.id)
    service.feed(b.id)
    harness = Harness(service)
    harness.session.usernames = {1: "alex_current", 2: "peer_current"}
    await harness.update(1, callback=f"react:like:{b.id}", username="alex_old")
    assert "После подтверждения" in harness.sent()[-1].text
    assert not service.matches(a.id)
    await harness.update(1, callback="match:enable", username="alex_old")
    await harness.update(1, callback=f"react:like:{b.id}", username="alex_old")
    assert not any("@alex_current" in message.text for message in harness.sent())
    assert not any(message.chat_id == 2 for message in harness.sent())
    await harness.update(2, callback="match:enable", username="peer_current")
    await harness.update(2, callback=f"react:like:{a.id}", username="peer_current")
    assert any(message.chat_id == 2 and "@alex_current" in message.text for message in harness.sent())
    assert any(message.chat_id == 1 and "@peer_current" in message.text for message in harness.sent())
    assert not any("@alex_old" in message.text for message in harness.sent())
    service.react(b.id, a.id, "block")
    await harness.update(1, "/matches", username="alex_current")
    assert "Здесь появятся взаимные симпатии" in harness.sent()[-1].text
    await harness.bot.session.close()


async def test_deleting_profile_removes_fsm_telegram_id(service):
    harness = Harness(service)
    await register(harness,with_photo=True)
    await harness.update(1, "/delete")
    await harness.update(1, callback="delete:yes")
    assert service.user_by_telegram(1) is None
    with service.db.session() as session:
        assert session.scalar(select(func.count()).select_from(FSMRecord)) == 0
    await harness.bot.session.close()


async def test_steam_link_import_and_quick_game_selection(service, users):
    from app.steam_public import PublicProfile
    class Parser:
        async def fetch(self, url):
            assert url == "https://steamcommunity.com/id/test"
            return PublicProfile("76561198000000001", url,
                                 games=[{"appid":999991,"name":"Test Game","playtime_forever":180}],
                                 coverage="library")
    user = users(1)
    harness = Harness(service, Parser())
    await harness.update(1, "/steam")
    assert "Пароль и API-ключ не нужны" in harness.sent()[-1].text
    await harness.update(1, "https://steamcommunity.com/id/test")
    saved = service.user(user.id)
    assert saved.steam_id == "76561198000000001" and not saved.steam_verified
    assert any(game.app_id == 999991 and game.minutes == 180 for game in saved.games)
    await harness.update(1, "/games")
    await harness.update(1, "Test Game")
    await harness.update(1, callback="game:999991")
    await harness.update(1, callback="game:save")
    assert any(game.app_id == 999991 and game.selected for game in service.user(user.id).games)
    await harness.bot.session.close()


async def test_photo_in_registration_and_own_profile(service):
    harness = Harness(service)
    await register(harness, with_photo=True)
    user = service.user_by_telegram(1)
    assert user.photo_file_id == "photo-original"
    assert service.public_profile(user)["has_photo"]
    assert "photo_file_id" not in service.public_profile(user)
    await harness.update(1,"/profile")
    photo = harness.photos_sent()[-1]
    assert photo.photo == "photo-original" and "Алекс" in photo.caption
    assert "&lt;и&gt;" in photo.caption
    await harness.bot.session.close()


async def test_photo_replace_remove_and_game_edit_preserves_photo(service, users):
    user = users(1)
    harness = Harness(service)
    await harness.update(1,"/photo")
    await harness.update(1,photos=photo_sizes("first-photo"))
    assert service.photo_file(user.id) == "first-photo"
    await harness.update(1,"/photo")
    await harness.update(1,photos=photo_sizes("second-photo"))
    assert service.photo_file(user.id) == "second-photo"
    await harness.update(1,"/games")
    await harness.update(1,callback="game:save")
    assert service.photo_file(user.id) == "second-photo"
    await harness.update(1,"/photo")
    await harness.update(1,callback="photo:remove")
    assert service.photo_file(user.id) is None
    assert "Фото удалено" in harness.sent()[-1].text
    await harness.update(1,"/profile")
    assert "Игрок 1" in harness.sent()[-1].text
    await harness.bot.session.close()


async def test_discovery_and_matches_show_profile_photo(service, users):
    a,b = users(1),users(2)
    service.set_photo(b.id,"peer-photo")
    harness = Harness(service)
    await harness.update(1,"/find")
    card = harness.photos_sent()[-1]
    assert card.photo == "peer-photo"
    assert any(button.callback_data == f"react:like:{b.id}"
               for row in card.reply_markup.inline_keyboard for button in row)
    service.feed(b.id)
    service.react(a.id,b.id,"like")
    service.react(b.id,a.id,"like")
    await harness.update(1,"/matches")
    assert len(harness.photos_sent()) == 2
    assert harness.photos_sent()[-1].photo == "peer-photo"
    await harness.bot.session.close()


async def test_photo_draft_survives_restart_before_registration_save(service):
    harness = Harness(service)
    await register(harness,with_photo=True)
    user = service.user_by_telegram(1)
    # Test a separate unfinished registration from the photo step through a restart.
    from app.bot import Form
    from aiogram.fsm.storage.base import StorageKey
    key = StorageKey(bot_id=harness.bot.id,chat_id=2,user_id=2)
    await harness.dp.storage.set_state(key,Form.photo)
    await harness.dp.storage.set_data(key,{
        "photo_flow":"registration", "form":{"name":"Новый игрок","age":25}, "selected":[]
    })
    await harness.update(2,photos=photo_sizes("draft-photo"))
    restarted = Harness(service)
    await restarted.update(2,callback="game:105600")
    await restarted.update(2,callback="game:save")
    assert service.user_by_telegram(2) is None
    await restarted.update(2,callback="review:publish")
    assert service.user_by_telegram(2).photo_file_id == "draft-photo"
    assert service.photo_file(user.id) == "photo-original"
    await harness.bot.session.close()
    await restarted.bot.session.close()


async def test_oversize_and_document_photo_rejected_without_changes(service,users):
    user = users(1)
    service.set_photo(user.id,"keep-this-photo")
    harness = Harness(service)
    await harness.update(1,"/photo")
    await harness.update(1,photos=photo_sizes("too-large",11*1024*1024))
    assert "слишком большое" in harness.sent()[-1].text
    await harness.update(1,document=Document(file_id="image-document",file_unique_id="doc",
                                           mime_type="image/jpeg",file_name="image.jpg"))
    assert "обычное фото" in harness.sent()[-1].text
    assert service.photo_file(user.id) == "keep-this-photo"
    await harness.update(1,callback="photo:keep")
    assert service.photo_file(user.id) == "keep-this-photo"
    await harness.bot.session.close()


async def test_long_caption_split_preserves_bio_and_actions(service,users):
    from app.models import Game
    games = [game.app_id for game in service.db.catalog()][:10]
    with service.db.session.begin() as session:
        for index,app_id in enumerate(games):
            session.get(Game,app_id).name = f"Игра {index} " + "🎮"*40
    users(1,game_ids=games,bio="я"*500)
    peer = users(2,game_ids=games,bio="я"*500)
    service.set_photo(peer.id,"long-card-photo")
    harness = Harness(service)
    await harness.update(1,"/find")
    photo = harness.photos_sent()[-1]
    assert len(photo.caption.encode("utf-16-le"))//2 <= 1024
    assert "я"*500 not in photo.caption
    await harness.update(1, callback=f"feed:details:{peer.id}")
    assert "я"*500 in harness.sent()[-1].text
    await harness.bot.session.close()


async def test_unavailable_telegram_photo_falls_back_to_text(service,users):
    user = users(1)
    service.set_photo(user.id,"stale-photo")
    harness = Harness(service)
    harness.session.fail_photos = True
    await harness.update(1,"/profile")
    assert "Игрок 1" in harness.sent()[-1].text
    assert service.photo_file(user.id) == "stale-photo"
    await harness.bot.session.close()


async def test_old_chat_button_does_not_restore_the_message_relay(service, users):
    a,b = users(1),users(2)
    service.set_photo(a.id,"original-profile-photo")
    service.feed(a.id)
    service.feed(b.id)
    service.react(a.id,b.id,"like")
    match = service.react(b.id,a.id,"like")
    harness = Harness(service)
    await harness.update(1,callback=f"chat:{match['match_id']}")
    await harness.update(1, "Эта реплика не будет переслана.")
    await harness.update(1,photos=photo_sizes("private-chat-photo"))
    assert service.photo_file(a.id) == "original-profile-photo"
    assert not harness.photos_sent()
    assert all(message.chat_id == 1 for message in harness.sent())
    await harness.bot.session.close()


async def test_imported_recent_games_visible_alongside_manual_favorites(service,users):
    from app.steam_public import PublicProfile
    class Parser:
        calls = 0
        async def fetch(self,url):
            self.calls += 1
            return PublicProfile("76561198000000001",url,games=[
                {"appid":105600,"name":"Terraria","playtime_2weeks":60},
                {"appid":548430,"name":"Deep Rock Galactic","playtime_2weeks":120},
                {"appid":570,"name":"Dota 2","playtime_2weeks":180},
            ],coverage="recent")
    user = users(1,game_ids=[1245620])
    parser = Parser()
    harness = Harness(service,parser)
    await harness.update(1,"/steam")
    await harness.update(1,"https://steamcommunity.com/id/test")
    await harness.update(1,"/profile")
    text = harness.sent()[-1].text
    assert all(name in text for name in ["ELDEN RING","Terraria","Deep Rock Galactic","Dota 2"])
    assert "Актуальные игры" in text and "Из Steam" in text
    profile = service.public_profile(service.user(user.id))
    assert profile["steam_game_count"] == 3
    assert len(profile["games"]) == 4
    assert profile["game_ids"] == [1245620]
    await harness.update(1,callback="steam:sync")
    cached = harness.sent()[-1].text
    assert "недавние игры: 3" in cached and "Полная библиотека не получена" in cached
    assert "Библиотека уже обновлена" not in cached
    assert parser.calls == 1
    await harness.bot.session.close()


async def test_failed_library_check_has_honest_status_and_short_retry(service,users):
    from app.models import User,utcnow
    from app.steam_public import PublicProfile
    from datetime import timedelta
    class Parser:
        calls = 0
        async def fetch(self,url):
            self.calls += 1
            return PublicProfile("76561198000000001",url,coverage="profile_only")
    user = users(1)
    parser = Parser()
    harness = Harness(service,parser)
    await harness.update(1,"/steam")
    await harness.update(1,"https://steamcommunity.com/id/test")
    assert "не загрузила список игр" in harness.sent()[-1].text
    await harness.update(1,callback="steam:sync")
    assert "не загрузила список игр" in harness.sent()[-1].text
    assert "через 1 мин" in harness.sent()[-1].text
    assert "Библиотека уже обновлена" not in harness.sent()[-1].text
    assert parser.calls == 1
    with service.db.session.begin() as session:
        session.get(User,user.id).steam_checked_at = utcnow() - timedelta(seconds=61)
    await harness.update(1,callback="steam:sync")
    assert parser.calls == 2
    await harness.bot.session.close()


async def test_test_gallery_shows_pinterest_photos_and_no_fake_match(service,users):
    from app.test_profiles import seed_test_profiles
    from app.models import Match
    user = users(1)
    seed_test_profiles(service)
    harness = Harness(service)
    await harness.update(1,"/test")
    first = harness.photos_sent()[-1]
    assert isinstance(first.photo,FSInputFile)
    like = next(button.callback_data for row in first.reply_markup.inline_keyboard for button in row
                if button.callback_data and button.callback_data.startswith("testreact:like:"))
    target_id = int(like.split(":")[-1])
    assert service.photo_file(target_id) == "uploaded-test-photo"
    await harness.update(1,callback=like)
    assert any("Тестовый лайк" in message.text for message in harness.sent())
    assert len(harness.photos_sent()) == 2
    assert all(method.chat_id == user.telegram_id for method in harness.sent()+harness.photos_sent())
    with service.db.session() as session:
        assert session.scalar(select(func.count()).select_from(Match)) == 0
    await harness.update(1,callback="test:restart")
    assert harness.photos_sent()[-1].photo == "uploaded-test-photo"
    await harness.bot.session.close()


async def test_static_test_profiles_appear_in_regular_search_without_answers(service,users):
    from app.test_profiles import seed_test_profiles
    from app.models import Match
    user = users(1)
    seed_test_profiles(service)
    harness = Harness(service)
    await harness.update(1,"/find")
    card = harness.photos_sent()[-1]
    assert "ДЕМО-ПРОФИЛЬ БОТА" in card.caption and "По расписанию" in card.caption
    callback = next(button.callback_data for row in card.reply_markup.inline_keyboard for button in row
                    if button.callback_data.startswith("react:like:"))
    assert not any(button.callback_data.startswith("react:report:")
                   for row in card.reply_markup.inline_keyboard for button in row)
    await harness.update(1,callback=callback,card_photo=True)
    replacement = next(call for call in harness.session.calls if isinstance(call, EditMessageMedia))
    assert len(harness.photos_sent()) == 1 and "ДЕМО-ПРОФИЛЬ БОТА" in replacement.media.caption
    assert not any("Тестовый лайк" in message.text for message in harness.sent())
    assert all(message.chat_id == user.telegram_id for message in harness.sent()+harness.photos_sent())
    with service.db.session() as session:
        assert session.scalar(select(func.count()).select_from(Match)) == 0
    await harness.bot.session.close()


async def test_edit_one_field_preserves_profile_and_steam_games(service, users):
    user = users(1, bio="Старый текст")
    service.set_photo(user.id, "original-photo")
    service.import_games(user.id, [{"appid":730, "name":"Counter-Strike 2", "playtime_forever":40}])
    harness = Harness(service)
    await harness.update(1, "/edit", username="alex")
    assert "Что хочешь изменить?" in harness.sent()[-1].text
    await harness.update(1, callback="edit:bio", username="alex")
    await harness.update(1, "Новый текст", username="alex")
    updated = service.public_profile(service.user(user.id))
    assert updated["bio"] == "Новый текст" and updated["age"] == user.age
    assert set(updated["game_ids"]) == {105600, 548430}
    assert updated["steam_games"] == ["Counter-Strike 2"]
    assert service.photo_file(user.id) == "original-photo"
    await harness.bot.session.close()


async def test_short_registration_goes_back_previews_and_publishes_without_photo(service):
    harness = Harness(service)
    await harness.update(1, "/start")
    await harness.update(1, callback="consent:yes")
    await harness.update(1, "Алекс")
    await harness.update(1, "25")
    await harness.update(1, callback="form:back")
    await harness.update(1, "26")
    await harness.update(1, callback="form:purpose:friends")
    await harness.update(1, callback="form:timezone:Asia/Novosibirsk")
    await harness.update(1, callback="form:hours:19-23")
    await harness.update(1, callback="game:105600")
    await harness.update(1, callback="game:save")
    assert "Алекс, 26" in harness.sent()[-1].text and "@username" in harness.sent()[-1].text
    assert service.user_by_telegram(1) is None
    await harness.update(1, callback="review:publish")
    saved = service.user_by_telegram(1)
    assert saved.age == 26 and saved.bio == "" and saved.photo_file_id is None
    assert saved.share_username and saved.username is None
    assert "добавь @username" in harness.sent()[-1].text
    await harness.bot.session.close()


async def test_skip_replaces_card_and_undo_restores_it_from_empty_feed(service, users):
    viewer, peer = users(1), users(2)
    harness = Harness(service)
    await harness.update(1, "/find")
    first_card = harness.sent()[-1].text
    await harness.update(1, callback=f"react:skip:{peer.id}", card_text=first_card, card_id=20)
    edits = [call for call in harness.session.calls if isinstance(call, EditMessageText)]
    assert edits[-1].message_id == 20 and "Новых подходящих анкет пока нет" in edits[-1].text
    options = {button.callback_data for row in edits[-1].reply_markup.inline_keyboard for button in row}
    assert {"feed:undo", "feed:alert_on", "edit:age_range", "edit:games"} <= options
    await harness.update(1, callback="feed:undo", card_text=edits[-1].text, card_id=20)
    restored = [call for call in harness.session.calls if isinstance(call, EditMessageText)][-1]
    assert restored.message_id == 20 and peer.name in restored.text
    assert len(harness.sent()) == 1
    assert service.undo_skip(viewer.id) is None
    await harness.bot.session.close()


async def test_play_now_filters_old_steam_library_and_keeps_mutual_filters(service, users):
    viewer = users(1)
    unrelated = users(2)
    service.import_games(unrelated.id, [{"appid":730, "playtime_forever":9999}])
    candidate = users(3, game_ids=[730])
    incompatible = users(4, game_ids=[730], age_min=40)
    service.set_current_game(candidate.id, 730)
    harness = Harness(service)
    await harness.update(1, "/play")
    await harness.update(1, "Counter")
    await harness.update(1, callback="focus:game:730", card_text="Выбери игру")
    focused_card = [call for call in harness.session.calls if isinstance(call, EditMessageText)][-1]
    assert "Тоже ищет компанию" in focused_card.text
    assert "по твоему времени" in focused_card.text
    assert candidate.name in focused_card.text
    result_ids = [row["profile"]["id"] for row in service.feed(viewer.id, record_impressions=False)]
    assert result_ids == [candidate.id] and incompatible.id not in result_ids
    await harness.update(1, callback="focus:clear", card_text=focused_card.text)
    assert service.current_game(service.user(viewer.id)) is None
    assert unrelated.id in [row["profile"]["id"] for row in service.feed(viewer.id, record_impressions=False)]
    await harness.bot.session.close()


async def test_empty_feed_alert_waits_for_a_real_candidate_and_only_sends_once(service, users):
    from app.test_profiles import seed_test_profiles

    viewer = users(1)
    harness = Harness(service)
    await harness.update(1, "/find")
    await harness.update(1, callback="feed:alert_on", card_text=harness.sent()[-1].text)
    seed_test_profiles(service)
    before = len(harness.sent())
    await deliver_profile_alerts(harness.bot, service)
    assert len(harness.sent()) == before
    assert service.user(viewer.id).notify_new_profiles
    users(2)
    await deliver_profile_alerts(harness.bot, service)
    await deliver_profile_alerts(harness.bot, service)
    assert len(harness.sent()) == before + 1 and "появилась подходящая анкета" in harness.sent()[-1].text
    assert not service.user(viewer.id).notify_new_profiles
    await harness.bot.session.close()
