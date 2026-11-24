from datetime import timedelta
from html import escape
from time import time

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext

from app.domain.errors import DomainError
from app.domain.fortnite import MODE_LABELS
from app.persistence.models import utcnow
from app.telegram.states import FortniteForm
from app.telegram.ui import buttons, require_profile

MODE_NAMES = MODE_LABELS | {
    "reload_combined": "Reload (общий ранг)",
    "arenas": "Arenas",
    "getaway": "The Getaway",
    "getaway_zero_build": "The Getaway Zero Build",
    "crown_jam": "Crown Jam",
}
REFRESH_INTERVAL = timedelta(minutes=15)


def mode_name(mode):
    if mode.startswith("team:") and mode not in MODE_NAMES:
        return "Другой формат команды"
    return escape(MODE_NAMES.get(mode, "Другой плейлист"))


def playtime(minutes):
    hours, remainder = divmod(minutes, 60)
    return f"{hours} ч {remainder} мин" if hours else f"{remainder} мин"


def profile_summary(profile):
    text = [f"<b>Fortnite · {escape(profile.display_name)}</b>"]
    if profile.total_minutes is not None:
        text.append(f"Наигрыш за всё время по доступной статистике: {playtime(profile.total_minutes)}.")
    else:
        text.append("Наигрыш за всё время недоступен.")
    modes = {entry.mode: entry for entry in profile.modes}
    if profile.favorite_mode:
        mode = modes[profile.favorite_mode]
        rank = mode.rank_name
        if not rank and mode.mode in {"reload", "reload_zero_build"} and "reload_combined" in modes:
            rank = modes["reload_combined"].rank_name
        details = ""
        if mode.recent_minutes is not None:
            details = f" · {playtime(mode.recent_minutes)}"
        elif mode.recent_matches is not None:
            details = f" · матчей: {mode.recent_matches}"
        text.append(f"Любимый режим за 30 дней: {mode_name(mode.mode)}{details}.")
        text.append(f"Ранг: {escape(rank) if rank else 'нет доступного ранга'}.")
    elif profile.recent_checked_at:
        text.append("В доступной статистике за 30 дней нет активности по режимам.")
    else:
        text.append("Любимый режим за 30 дней пока недоступен.")
    if profile.recent_checked_at:
        text.append(f"Статистика за 30 дней на {profile.recent_checked_at:%d.%m.%Y} (UTC).")
    mode_rows = []
    team_rows = []
    for entry in profile.modes:
        if entry.lifetime_minutes is None and not entry.rank_name:
            continue
        if entry.mode.startswith("team:"):
            team_rows.append(entry)
        elif entry.mode in MODE_NAMES:
            mode_rows.append(entry)
    for heading, entries in (("Статистика по режимам", mode_rows), ("Наигрыш по формату команды", team_rows)):
        if entries:
            text.append(f"<b>{heading}</b>")
        for entry in entries[:12]:
            values = [
                playtime(entry.lifetime_minutes) if entry.lifetime_minutes is not None else None,
                escape(entry.rank_name) if entry.rank_name else None,
            ]
            text.append(f"• {mode_name(entry.mode)} · " + " · ".join(value for value in values if value))
    return "\n".join(text)


def build_fortnite_router(profiles, fortnite, fetch_fortnite_profile):
    router = Router(name="fortnite")

    async def show(message, profile):
        await message.answer(
            profile_summary(profile),
            reply_markup=buttons(
                [
                    [("Обновить статистику", "fortnite:refresh")],
                    [("Сменить ник", "fortnite:link"), ("Отключить", "fortnite:disconnect")],
                ]
            ),
        )

    async def ask_nickname(message, state):
        await state.set_state(FortniteForm.nickname)
        await message.answer(
            "Отправь отображаемое имя Epic Games. Бот получит доступную публичную статистику."
        )

    async def load(message, state, user_id, nickname, *, account_id=None):
        nickname = nickname.strip()
        if (
            not 2 <= len(nickname) <= 32
            or not nickname.isprintable()
            or any(char in nickname for char in "/\\")
        ):
            await message.answer("Укажи ник Epic Games без ссылки (от 2 до 32 символов).")
            return
        attempt = time()
        state_data = await state.get_data()
        if attempt - state_data.get("fortnite_attempt_at", 0) < 15:
            await message.answer("Подожди несколько секунд перед повторной проверкой Fortnite.")
            return
        await state.update_data(fortnite_attempt_at=attempt)
        try:
            snapshot = await fetch_fortnite_profile(nickname, account_id=account_id)
            if (
                account_id
                and snapshot.playtime is None
                and snapshot.activity is None
                and snapshot.ranks is None
            ):
                raise DomainError(
                    "Fortnite временно не отдал статистику. Последняя сохранённая проверка доступна в меню."
                )
            profile = fortnite.save(user_id, snapshot, expected_account_id=account_id)
        except DomainError as error:
            await message.answer(escape(str(error)))
            return
        await state.set_state(None)
        await show(message, profile)

    @router.message(Command("fortnite"))
    async def fortnite_command(message, state: FSMContext, command: CommandObject):
        user = await require_profile(profiles, message)
        if user:
            if command.args:
                await load(message, state, user.id, command.args)
            elif profile := fortnite.get(user.id):
                await show(message, profile)
            else:
                await ask_nickname(message, state)

    @router.callback_query(F.data.in_({"fortnite:link", "fortnite:refresh", "fortnite:disconnect"}))
    async def fortnite_action(callback, state: FSMContext):
        await callback.answer()
        user = profiles.user_by_telegram(callback.from_user.id)
        if not user or user.banned:
            return
        message = callback.message
        if callback.data == "fortnite:link":
            await ask_nickname(message, state)
        elif callback.data == "fortnite:disconnect":
            fortnite.disconnect(user.id)
            await state.set_state(None)
            await message.answer("Fortnite отключён. Импортированная статистика удалена.")
        elif profile := fortnite.get(user.id):
            if utcnow() - profile.checked_at < REFRESH_INTERVAL:
                await message.answer("Статистика обновляется раз в 15 минут.")
                await show(message, profile)
            else:
                await load(message, state, user.id, profile.display_name, account_id=profile.account_id)
        else:
            await ask_nickname(message, state)

    @router.message(FortniteForm.nickname, F.text, ~F.text.startswith("/"))
    async def fortnite_nickname(message, state: FSMContext):
        user = await require_profile(profiles, message)
        if user:
            await load(message, state, user.id, message.text)

    return router
