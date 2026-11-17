from datetime import datetime, timezone

from aiogram.client.session.base import BaseSession
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import AnswerCallbackQuery, EditMessageReplyMarkup, SendMessage, SendPhoto
from aiogram.types import CallbackQuery, Chat, Document, FSInputFile, Message, PhotoSize, Update, User as TelegramUser
from sqlalchemy import func, select

from app.bot import build_bot
from app.config import Settings
from app.models import Event, FSMRecord


class FakeSession(BaseSession):
    def __init__(self):
        super().__init__()
        self.calls = []
        self.fail_photos = False

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
        if isinstance(method, (AnswerCallbackQuery, EditMessageReplyMarkup)):
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

    async def update(self, user_id, text=None, callback=None, username=None, photos=None, document=None):
        self.counter += 1
        sender = TelegramUser(id=user_id, is_bot=False, first_name="Игрок", username=username)
        message = Message(message_id=self.counter, date=datetime.now(timezone.utc),
                          chat=Chat(id=user_id, type="private"), from_user=sender, text=text,
                          photo=photos, document=document)
        if callback:
            message = message.model_copy(update={"from_user":TelegramUser(id=self.bot.id, is_bot=True, first_name="PartyUp")})
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
    await harness.update(telegram, callback="form:male")
    await harness.update(telegram, callback="form:any")
    await harness.update(telegram, callback="form:both")
    await harness.update(telegram, "18-35")
    await harness.update(telegram, callback="form:ru")
    await harness.update(telegram, callback="form:Asia/Novosibirsk")
    await harness.update(telegram, "22-2")
    await harness.update(telegram, "За спокойный кооп <и> разговоры")
    if with_photo:
        await harness.update(telegram, photos=photo_sizes())
    else:
        await harness.update(telegram, callback="photo:keep")
    await harness.update(telegram, callback="game:105600")
    await harness.update(telegram, callback="game:save", username="alex")


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
    assert "Как указать твой пол" in second.sent()[-1].text
    await first.bot.session.close()
    await second.bot.session.close()


async def test_relay_contact_consent_and_blocking(service, users):
    a, b = users(1), users(2)
    service.feed(a.id)
    service.feed(b.id)
    service.react(a.id, b.id, "like")
    result = service.react(b.id, a.id, "like")
    harness = Harness(service)
    await harness.update(1, callback=f"chat:{result['match_id']}")
    await harness.update(1, "Привет <script>private-token</script>")
    delivered = [message for message in harness.sent() if message.chat_id == 2]
    assert len(delivered) == 1 and "&lt;script&gt;" in delivered[0].text
    assert "@player" not in delivered[0].text
    with service.db.session() as session:
        events = list(session.scalars(select(Event).where(Event.kind == "message_sent")))
        assert events and "private-token" not in str(events[0].details)
    await harness.update(1, callback=f"contact:{result['match_id']}", username="alex")
    assert "@alex" in [message for message in harness.sent() if message.chat_id == 2][-1].text
    service.react(b.id, a.id, "block")
    sent_before = len([message for message in harness.sent() if message.chat_id == 2])
    await harness.update(1, "Это уже не должно дойти")
    assert len([message for message in harness.sent() if message.chat_id == 2]) == sent_before
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
    assert photo.reply_markup is None
    assert "я"*500 in harness.sent()[-1].text
    assert harness.sent()[-1].reply_markup is not None
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


async def test_photo_does_not_overwrite_profile_while_chatting(service, users):
    a,b = users(1),users(2)
    service.set_photo(a.id,"original-profile-photo")
    service.feed(a.id)
    service.feed(b.id)
    service.react(a.id,b.id,"like")
    match = service.react(b.id,a.id,"like")
    harness = Harness(service)
    await harness.update(1,callback=f"chat:{match['match_id']}")
    await harness.update(1,photos=photo_sizes("private-chat-photo"))
    assert service.photo_file(a.id) == "original-profile-photo"
    assert not harness.photos_sent()
    assert "В переписке" in harness.sent()[-1].text
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
    assert "ТЕСТОВАЯ АНКЕТА" in first.caption
    assert "вымышлены" in first.caption
    assert "Pinterest" in first.caption
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
    assert "ТЕСТОВАЯ АНКЕТА" in card.caption and "Оценка сходства" in card.caption
    callback = next(button.callback_data for row in card.reply_markup.inline_keyboard for button in row
                    if button.callback_data.startswith("react:like:"))
    assert not any(button.callback_data.startswith("react:report:")
                   for row in card.reply_markup.inline_keyboard for button in row)
    await harness.update(1,callback=callback)
    assert len(harness.photos_sent()) == 2
    assert "Оценка сходства" in harness.photos_sent()[-1].caption
    assert not any("Тестовый лайк" in message.text for message in harness.sent())
    assert all(message.chat_id == user.telegram_id for message in harness.sent()+harness.photos_sent())
    with service.db.session() as session:
        assert session.scalar(select(func.count()).select_from(Match)) == 0
    await harness.bot.session.close()
