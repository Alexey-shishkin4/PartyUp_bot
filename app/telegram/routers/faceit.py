from datetime import timedelta
from html import escape
from time import time

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext

from app.domain.errors import DomainError
from app.domain.faceit import FaceitLookup
from app.persistence.models import utcnow
from app.telegram.states import FaceitForm
from app.telegram.ui import buttons, require_profile


REFRESH_INTERVAL = timedelta(minutes=15)
CALLBACKS = {"faceit:refresh", "faceit:link", "faceit:steam", "faceit:disconnect"}


def profile_text(profile):
    values = [
        ("Уровень FACEIT", f"{profile.skill_level}/10" if profile.skill_level is not None else None),
        ("Elo", profile.elo),
        ("Матчи CS2", profile.match_count),
        ("Победы", profile.wins),
        ("Процент побед", f"{profile.win_rate:g}%" if profile.win_rate is not None else None),
    ]
    text = [f"<b>CS2 · FACEIT · {escape(profile.nickname)}</b>"]
    text.extend(f"{label}: {value if value is not None else 'нет данных'}" for label, value in values)
    text.append(f"Источник: FACEIT Data API · {profile.checked_at:%d.%m.%Y %H:%M} UTC")
    return "\n".join(text)


def build_faceit_router(profiles, faceit, fetch_faceit_profile):
    router = Router(name="faceit")

    async def show(message, profile):
        await message.answer(
            profile_text(profile),
            reply_markup=buttons([
                [("Обновить статистику", "faceit:refresh")],
                [("Сменить профиль", "faceit:link"), ("Отключить", "faceit:disconnect")],
            ]),
        )

    async def ask_account(message, state, user):
        await state.set_state(FaceitForm.account)
        await message.answer(
            "Отправь никнейм FACEIT, ссылку на профиль FACEIT или SteamID64. "
            "Бот передаст идентификатор в FACEIT и сохранит публичную статистику CS2 для твоего чата.",
            reply_markup=buttons([[("Найти по подключённому Steam", "faceit:steam")]]) if user.steam_id else None,
        )

    async def load(message, state, user, lookup, existing):
        now = time()
        if now - (await state.get_data()).get("faceit_attempt_at", 0) < 15:
            await message.answer("Подожди 15 секунд между запросами к FACEIT.")
            return
        await state.update_data(faceit_attempt_at=now)
        try:
            stats = await fetch_faceit_profile(lookup)
            profile = faceit.save(
                user.id, stats, expected_checked_at=existing.checked_at if existing else None
            )
        except DomainError as error:
            await message.answer(escape(str(error)))
            return
        await state.set_state(None)
        await show(message, profile)

    async def link(message, state, user, text, existing):
        try:
            lookup = FaceitLookup.parse(text)
        except DomainError as error:
            await message.answer(escape(str(error)))
            return
        if (
            existing
            and lookup.kind == "nickname"
            and lookup.value.casefold() == existing.nickname.casefold()
            and utcnow() - existing.checked_at < REFRESH_INTERVAL
        ):
            await state.set_state(None)
            await show(message, existing)
            return
        await load(message, state, user, lookup, existing)

    @router.message(Command("faceit", "cs2"))
    async def faceit_command(message, state: FSMContext, command: CommandObject):
        user = await require_profile(profiles, message)
        if not user:
            return
        existing = faceit.get(user.id)
        if command.args:
            await link(message, state, user, command.args, existing)
        elif existing:
            await show(message, existing)
        else:
            await ask_account(message, state, user)

    @router.message(FaceitForm.account, F.text, ~F.text.startswith("/"))
    async def faceit_account(message, state: FSMContext):
        user = await require_profile(profiles, message)
        if user:
            await link(message, state, user, message.text, faceit.get(user.id))

    @router.callback_query(F.data.in_(CALLBACKS))
    async def faceit_action(callback, state: FSMContext):
        await callback.answer()
        user = profiles.user_by_telegram(callback.from_user.id)
        if not user or user.banned:
            return
        message = callback.message
        existing = faceit.get(user.id)
        if callback.data == "faceit:disconnect":
            faceit.disconnect(user.id)
            if await state.get_state() == FaceitForm.account.state:
                await state.set_state(None)
            await message.answer("Профиль FACEIT отключён. Сохранённая статистика удалена.")
        elif callback.data == "faceit:steam" and user.steam_id:
            await load(message, state, user, FaceitLookup("steam_id", user.steam_id), existing)
        elif callback.data != "faceit:refresh" or existing is None:
            await ask_account(message, state, user)
        elif utcnow() - existing.checked_at < REFRESH_INTERVAL:
            await message.answer("Статистика FACEIT обновляется раз в 15 минут.")
            await show(message, existing)
        else:
            await load(message, state, user, FaceitLookup("player_id", existing.player_id), existing)

    return router
