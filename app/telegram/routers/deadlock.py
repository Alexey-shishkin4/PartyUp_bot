from datetime import timedelta
from html import escape
from time import time

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext

from app.domain.deadlock import rank_name
from app.domain.errors import DomainError
from app.persistence.models import utcnow
from app.telegram.ui import buttons, playtime, require_profile


REFRESH_INTERVAL = timedelta(minutes=15)
INCOMPLETE_REFRESH_INTERVAL = timedelta(minutes=1)


def profile_text(profile):
    rank = rank_name(profile.rank_badge)
    lines = ["<b>Deadlock</b>"]
    if rank:
        lines.append(f"Ранг Valve после последнего доступного рейтингового матча: {rank}")
        if profile.ranked_at:
            lines.append(f"Дата матча: {profile.ranked_at:%d.%m.%Y %H:%M} UTC")
    else:
        lines.append("Ранг Valve в доступных рейтинговых матчах не найден.")
    if profile.match_seconds is not None:
        lines.append(f"Время в матчах по доступной истории: ≈ {playtime(profile.match_seconds // 60)}")
    if profile.match_count is not None:
        lines.append(f"Матчи в доступной истории: {profile.match_count}")
    else:
        lines.append("Игровое время и количество матчей недоступны.")
    lines.append(f"Deadlock API · проверено {profile.checked_at:%d.%m.%Y %H:%M} UTC")
    return "\n".join(lines)


def build_deadlock_router(profiles, deadlock, fetch_deadlock_profile):
    router = Router(name="deadlock")

    async def show(message, state, user):
        if not user.steam_id:
            await message.answer("Подключи профиль Steam через /steam, затем открой /deadlock.")
            return
        profile = deadlock.get(user.id, user.steam_id)
        interval = REFRESH_INTERVAL if profile and profile.match_count is not None else INCOMPLETE_REFRESH_INTERVAL
        if not profile or utcnow() - profile.checked_at >= interval:
            now = time()
            if now - (await state.get_data()).get("deadlock_attempt_at", 0) < 15:
                await message.answer("Подожди 15 секунд между запросами статистики Deadlock.")
                return
            await state.update_data(deadlock_attempt_at=now)
            try:
                stats = await fetch_deadlock_profile(user.steam_id)
                profile = deadlock.save(
                    user.id, user.steam_id, stats, expected_checked_at=profile.checked_at if profile else None,
                )
            except DomainError as error:
                await message.answer(escape(str(error)))
                return
        await message.answer(
            profile_text(profile),
            reply_markup=buttons([[("Обновить статистику", "deadlock:refresh")]]),
        )

    @router.message(Command("deadlock"))
    async def deadlock_command(message, state: FSMContext):
        user = await require_profile(profiles, message)
        if user:
            await show(message, state, user)

    @router.callback_query(F.data == "deadlock:refresh")
    async def deadlock_refresh(callback, state: FSMContext):
        await callback.answer()
        user = profiles.user_by_telegram(callback.from_user.id)
        if user and not user.banned:
            await show(callback.message, state, user)

    return router
