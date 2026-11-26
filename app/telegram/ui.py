from aiogram.exceptions import TelegramBadRequest
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup

CONTACT_NOTICE = "При взаимном лайке бот покажет собеседнику твой Telegram @username."
MAX_PHOTO_SIZE = 10 * 1024 * 1024


def playtime(minutes):
    hours, remainder = divmod(minutes, 60)
    return f"{hours} ч {remainder} мин" if hours else f"{remainder} мин"


def buttons(rows):
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=text, callback_data=data) for text, data in row] for row in rows
        ]
    )


def menu():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🔎 Найти своих"), KeyboardButton(text="💜 Совпадения")],
            [KeyboardButton(text="👤 Моя анкета"), KeyboardButton(text="🎮 Steam")],
            [KeyboardButton(text="🎮 Хочу сыграть сейчас")],
        ],
        resize_keyboard=True,
    )


def avatar_buttons(*, back=None, allow_removal=False):
    rows = [[("Загрузить фото", "photo:upload")], [("Выбрать стандартное", "photo:default")]]
    if allow_removal:
        rows.append([("Удалить фото из анкеты", "photo:remove")])
    if back:
        rows.append([("← Назад", back)])
    return buttons(rows)


async def clear_buttons(message):
    try:
        await message.edit_reply_markup(reply_markup=None)
    except TelegramBadRequest:
        pass


async def require_profile(profiles, message):
    user = profiles.user_by_telegram(message.from_user.id)
    if not user:
        await message.answer("Сначала заполни анкету: /start")
        return None
    if user.banned:
        await message.answer("Анкета заблокирована модератором. Удалить данные: /delete")
        return None
    return user
