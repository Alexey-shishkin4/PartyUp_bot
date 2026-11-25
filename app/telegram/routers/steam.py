import math
from datetime import timedelta
from html import escape

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.domain.errors import DomainError
from app.persistence.models import utcnow
from app.telegram.states import Form
from app.telegram.steam_ui import steam_attempt_allowed, steam_status_text
from app.telegram.ui import menu, require_profile


def build_steam_router(profiles, steam, fetch_steam_profile):
    router = Router(name="steam")

    @router.message(Command("steam"))
    @router.message(F.text == "🎮 Steam")
    async def steam_menu(message, state: FSMContext):
        user = await require_profile(profiles, message)
        if not user:
            return
        rows = [
            [InlineKeyboardButton(text="Добавить / изменить ссылку", callback_data="steam:add")],
            [InlineKeyboardButton(text="Обновить библиотеку", callback_data="steam:sync")],
            [InlineKeyboardButton(text="Отключить Steam и удалить импорт", callback_data="steam:disconnect")],
        ]
        await state.set_state(Form.steam_link)
        status = "Ссылка Steam сохранена." if user.steam_id else "Добавим игровой профиль по ссылке."
        status += "\n" + steam_status_text(profiles.public_profile(user))
        await message.answer(
            status + "\n\nПришли ссылку вида https://steamcommunity.com/id/ник или "
            "https://steamcommunity.com/profiles/SteamID. Получим доступные открытые игры "
            "и публичную статистику Dota 2 из OpenDota. "
            "Если Steam требует входа или скрывает библиотеку, выбрать игры можно через /games. "
            "Пароль и API-ключ не нужны. /cancel — вернуться.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
        )

    async def import_public(message, user, value, *, refresh=False):
        profile = await fetch_steam_profile(value)
        steam.save_public_steam(user.id, profile, expected_steam_id=user.steam_id if refresh else None)
        saved = profiles.public_profile(profiles.user(user.id))
        text = "Ссылка Steam сохранена.\n" + steam_status_text(saved)
        if profile.warning:
            text += "\n" + profile.warning
        if saved["dota_match_estimate"]:
            text += "\nДля Dota 2 учтена оценка наигрыша по данным OpenDota."
        text += "\n/profile — посмотреть анкету. /games — выбрать актуальные игры."
        await message.answer(escape(text), reply_markup=menu())

    @router.callback_query(F.data.startswith("steam:"))
    async def steam_action(callback, state: FSMContext):
        await callback.answer()
        user = profiles.user_by_telegram(callback.from_user.id)
        if not user or user.banned:
            return
        if callback.data == "steam:add":
            await state.set_state(Form.steam_link)
            await callback.message.answer(
                "Пришли ссылку на свой профиль steamcommunity.com. /cancel — вернуться."
            )
            return
        if callback.data == "steam:disconnect":
            steam.disconnect_steam(user.id)
            await state.clear()
            await callback.message.answer("Steam и импорт удалены. Вручную выбранные игры сохранены.")
            return
        if not user.steam_id:
            await callback.message.answer("Сначала подключи Steam: /steam")
            return
        interval = timedelta(minutes=1 if user.steam_import_status in {"profile_only", "error"} else 15)
        if user.steam_checked_at:
            remaining = interval - (utcnow() - user.steam_checked_at)
            if remaining.total_seconds() > 0:
                wait_minutes = max(1, math.ceil(remaining.total_seconds() / 60))
                await callback.message.answer(
                    steam_status_text(profiles.public_profile(user))
                    + f"\nСледующая проверка доступна через {wait_minutes} мин."
                )
                return
        try:
            await import_public(
                callback.message, user, "https://steamcommunity.com/profiles/" + user.steam_id, refresh=True
            )
            await state.clear()
        except DomainError as error:
            await callback.message.answer(escape(str(error)))

    @router.message(Form.steam_link, F.text, ~F.text.startswith("/"))
    async def steam_link_message(message, state: FSMContext):
        user = await require_profile(profiles, message)
        if not user:
            return
        if not await steam_attempt_allowed(message, state):
            return
        try:
            await import_public(message, user, message.text)
            await state.clear()
        except DomainError as error:
            await message.answer(escape(str(error)))

    return router
