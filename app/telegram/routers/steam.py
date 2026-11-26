import math
from datetime import timedelta
from html import escape

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext

from app.domain.errors import DomainError
from app.persistence.models import utcnow
from app.telegram.states import Form
from app.telegram.steam_ui import steam_attempt_allowed, steam_status_text
from app.telegram.ui import buttons, menu, require_profile


def build_steam_router(profiles, steam, fetch_steam_profile, game_flow):
    router = Router(name="steam")

    @router.message(Command("steam"))
    @router.message(F.text == "🎮 Steam")
    async def steam_menu(message, state: FSMContext):
        user = await require_profile(profiles, message)
        if not user:
            return
        await show_steam(message, state, user)

    async def show_steam(message, state, user):
        await state.set_state(Form.steam_link)
        rows = [[("Пропустить", "accounts:open")]]
        status = "Добавим игровой профиль по ссылке."
        if user.steam_id:
            status = "Ссылка Steam сохранена.\n" + steam_status_text(profiles.public_profile(user, linked_games={}))
            rows = [
                [("Обновить библиотеку и ранги", "steam:sync")],
                [("Отключить Steam и удалить импорт", "steam:disconnect")],
                [("← Добавить игры", "accounts:open")],
            ]
        await message.answer(
            status + "\n\nПришли ссылку вида https://steamcommunity.com/id/ник или "
            "https://steamcommunity.com/profiles/SteamID. Загрузим доступные игры и ранги. "
            "Пароль и API-ключ не нужны.",
            reply_markup=buttons(rows),
        )

    async def import_public(message, state, user, value, *, refresh=False):
        profile = await fetch_steam_profile(value)
        steam.save_public_steam(user.id, profile, expected_steam_id=user.steam_id if refresh else None)
        user = profiles.user(user.id)
        saved = profiles.public_profile(user, linked_games={})
        text = "Ссылка Steam сохранена.\n" + steam_status_text(saved)
        if profile.warning:
            text += "\n" + profile.warning
        if saved["dota_match_estimate"]:
            text += "\nДля Dota 2 учтена оценка наигрыша по данным OpenDota."
        await state.clear()
        await message.answer(escape(text), reply_markup=menu())
        await game_flow.steam_attached(message, state, user, guide=not refresh)

    @router.callback_query(F.data.startswith("steam:"))
    async def steam_action(callback, state: FSMContext):
        await callback.answer()
        user = profiles.user_by_telegram(callback.from_user.id)
        if not user or user.banned:
            return
        if callback.data in {"steam:add", "steam:open"}:
            await show_steam(callback.message, state, user)
            return
        if callback.data == "steam:disconnect":
            steam.disconnect_steam(user.id)
            await state.clear()
            await callback.message.answer("Steam и импорт удалены. Вручную выбранные игры сохранены.")
            return
        if callback.data != "steam:sync":
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
                    steam_status_text(profiles.public_profile(user, linked_games={}))
                    + f"\nСледующая проверка доступна через {wait_minutes} мин."
                )
                return
        try:
            await import_public(
                callback.message, state, user, "https://steamcommunity.com/profiles/" + user.steam_id, refresh=True
            )
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
            await import_public(message, state, user, message.text)
        except DomainError as error:
            await message.answer(escape(str(error)))

    return router
