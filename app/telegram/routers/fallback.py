import logging

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError

from app.telegram.states import Form
from app.telegram.ui import menu

log = logging.getLogger(__name__)


def build_fallback_router():
    router = Router(name="fallback")

    @router.message(Form.photo)
    async def unsupported_photo(message):
        await message.answer(
            "Нужно обычное фото: скрепка → Фото или видео. "
            "Изображения файлом, видео и ссылки пока не подходят. "
            "Можно выбрать стандартный аватар в меню или выйти через /cancel."
        )

    @router.message(F.photo)
    async def photo_hint(message):
        await message.answer(
            "Добавить или заменить фото анкеты можно через /photo. "
            "При регистрации бот предложит выбрать аватар перед подключением Steam."
        )

    @router.message()
    async def fallback(message):
        await message.answer(
            "Используй меню или /start. /cancel — выйти из заполнения анкеты.", reply_markup=menu()
        )

    @router.callback_query()
    async def stale(callback):
        await callback.answer("Эта кнопка устарела. Открой меню заново.")

    @router.errors()
    async def on_error(event):
        # Do not log Update objects: they may include private message contents.
        log.error("Bot handler failed: %s", type(event.exception).__name__)
        if event.update.message:
            try:
                await event.update.message.answer(
                    "Не получилось выполнить действие. Повтори или вернись в меню: /cancel"
                )
            except TelegramAPIError:
                pass
        return True

    return router
