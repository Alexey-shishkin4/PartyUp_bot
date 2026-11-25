from datetime import datetime, timezone

from aiogram.client.session.base import BaseSession
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import (
    AnswerCallbackQuery,
    DeleteMessage,
    EditMessageMedia,
    EditMessageReplyMarkup,
    EditMessageText,
    GetChat,
    SendMessage,
    SendPhoto,
)
from aiogram.types import CallbackQuery, Chat, ChatFullInfo, InputFile, Message, PhotoSize, Update
from aiogram.types import User as TelegramUser

from app.config import Settings
from app.domain.errors import DomainError
from app.telegram.dispatcher import build_bot


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
            return Message(
                message_id=len(self.calls),
                date=datetime.now(timezone.utc),
                chat=Chat(id=int(method.chat_id), type="private"),
                text=method.text,
                from_user=TelegramUser(id=bot.id, is_bot=True, first_name="PartyUp"),
            )
        if isinstance(method, SendPhoto):
            if self.fail_photos:
                raise TelegramBadRequest(method=method, message="wrong file identifier")
            file_id = "uploaded-test-photo" if isinstance(method.photo, InputFile) else str(method.photo)
            return Message(
                message_id=len(self.calls),
                date=datetime.now(timezone.utc),
                chat=Chat(id=int(method.chat_id), type="private"),
                caption=method.caption,
                photo=[PhotoSize(file_id=file_id, file_unique_id="test-unique", width=1280, height=960)],
                from_user=TelegramUser(id=bot.id, is_bot=True, first_name="PartyUp"),
            )
        if isinstance(method, GetChat):
            return ChatFullInfo(
                id=int(method.chat_id),
                type="private",
                username=self.usernames.get(method.chat_id),
                accent_color_id=0,
                max_reaction_count=0,
                accepted_gift_types={
                    "unlimited_gifts": False,
                    "limited_gifts": False,
                    "unique_gifts": False,
                    "premium_subscription": False,
                    "gifts_from_channels": False,
                },
            )
        if isinstance(method, EditMessageMedia) and self.fail_photos:
            raise TelegramBadRequest(method=method, message="wrong file identifier")
        if isinstance(
            method,
            (AnswerCallbackQuery, DeleteMessage, EditMessageMedia, EditMessageReplyMarkup, EditMessageText),
        ):
            return True
        raise AssertionError(f"Unexpected network operation: {type(method).__name__}")

    async def stream_content(self, url, headers=None, timeout=30, chunk_size=65536, raise_for_status=True):
        yield b""


class Harness:
    def __init__(self, service, public_steam=None, developer_ids="", admin_ids="", steam=None, fortnite=None):
        settings = Settings(
            _env_file=None,
            bot_token="123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijk",
            developer_ids=developer_ids,
            admin_ids=admin_ids,
        )
        self.settings = settings

        async def fetch_profile(value):
            if steam is not None:
                return await steam.fetch_profile(value, public_steam)
            if public_steam is not None:
                return await public_steam.fetch(value)
            raise DomainError("Парсер Steam временно недоступен")

        self.bot, self.dp = build_bot(service, settings, fetch_profile, fortnite)
        self.session = FakeSession()
        self.bot.session = self.session
        self.counter = 0

    async def update(
        self,
        user_id,
        text=None,
        callback=None,
        username=None,
        photos=None,
        document=None,
        card_text=None,
        card_photo=False,
        card_id=None,
    ):
        self.counter += 1
        sender = TelegramUser(id=user_id, is_bot=False, first_name="Игрок", username=username)
        message = Message(
            message_id=card_id or self.counter,
            date=datetime.now(timezone.utc),
            chat=Chat(id=user_id, type="private"),
            from_user=sender,
            text=text,
            photo=photos,
            document=document,
        )
        if callback:
            message = message.model_copy(
                update={
                    "from_user": TelegramUser(id=self.bot.id, is_bot=True, first_name="PartyUp"),
                    "text": card_text,
                    "photo": photo_sizes() if card_photo else None,
                }
            )
            update = Update(
                update_id=self.counter,
                callback_query=CallbackQuery(
                    id=str(self.counter),
                    from_user=sender,
                    chat_instance="test",
                    message=message,
                    data=callback,
                ),
            )
        else:
            update = Update(update_id=self.counter, message=message)
        return await self.dp.feed_update(self.bot, update)

    def sent(self):
        return [call for call in self.session.calls if isinstance(call, SendMessage)]

    def photos_sent(self):
        return [call for call in self.session.calls if isinstance(call, SendPhoto)]


def photo_sizes(file_id="photo-original", file_size=200000):
    return [
        PhotoSize(file_id="photo-small", file_unique_id="small", width=320, height=240, file_size=1000),
        PhotoSize(file_id=file_id, file_unique_id="large", width=1280, height=960, file_size=file_size),
    ]


async def register(harness, telegram=1, with_photo=False):
    await harness.update(telegram, "/start")
    assert "Имя или ник" in harness.sent()[-1].text
    await harness.update(telegram, "Алекс")
    await harness.update(telegram, "17")
    assert "18 до 99" in harness.sent()[-1].text
    await harness.update(telegram, "25")
    assert "часовой пояс" in harness.sent()[-1].text
    await harness.update(telegram, callback="form:timezone:Asia/Novosibirsk")
    assert "Расскажи о себе" in harness.sent()[-1].text
    await harness.update(telegram, "За спокойный кооп <и> разговоры")
    avatar_options = [
        button.text for row in harness.sent()[-1].reply_markup.inline_keyboard for button in row
    ]
    assert avatar_options[:2] == ["Загрузить фото", "Выбрать стандартное"]
    if with_photo:
        await harness.update(telegram, callback="photo:upload")
        await harness.update(telegram, photos=photo_sizes())
    else:
        await harness.update(telegram, callback="photo:default")
    await harness.update(telegram, callback="registration:manual")
    await harness.update(telegram, callback="game:105600")
    await harness.update(telegram, callback="game:save")
    assert "Опубликовать" in str(harness.photos_sent()[-1].reply_markup)
    await harness.update(telegram, callback="review:publish", username="alex")
