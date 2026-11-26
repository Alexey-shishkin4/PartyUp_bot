from datetime import timedelta
from html import escape
from time import time

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext

from app.domain.errors import DomainError
from app.domain.pubg import MODES, parse_account
from app.persistence.models import utcnow
from app.telegram.states import PubgForm
from app.telegram.ui import buttons, playtime, require_profile


PLAYTIME_INTERVAL = timedelta(hours=6)
RANK_INTERVAL = timedelta(minutes=15)
SOURCE_NAMES = {"official": "PUBG API", "public": "pubg-api.org"}


def profile_text(profile):
    lines = [f"<b>PUBG · {escape(profile.nickname)}</b>", f"Платформа: {escape(profile.platform)}"]
    modes = profile.mode_seconds or {}
    if modes:
        lines.append(f"Время выживания по доступным режимам: {playtime(sum(modes.values()) // 60)}")
        lines.append(f"Наигрыш · {SOURCE_NAMES[profile.playtime_source]} · {profile.playtime_at:%d.%m.%Y %H:%M} UTC")
    else:
        lines.append("Время выживания недоступно.")
    for mode in MODES:
        value = playtime(modes[mode] // 60) if mode in modes else "нет данных"
        lines.append(f"• {mode} · {value}")
    if profile.rank_at:
        if profile.season_id:
            lines.append(f"Ранг текущего сезона {escape(profile.season_id.rsplit('-', 1)[-1])}")
        else:
            lines.append("Ранг по данным pubg-api.org · сезон не указан")
        if profile.ranks:
            lines.extend(f"• {mode} · {escape(profile.ranks[mode])}" for mode in MODES if mode in profile.ranks)
        else:
            lines.append("Источник не вернул ранг для доступных очередей.")
        lines.append(f"Проверка ранга · {SOURCE_NAMES[profile.rank_source]} · {profile.rank_at:%d.%m.%Y %H:%M} UTC")
    else:
        lines.append("Ранг пока недоступен.")
    return "\n".join(lines)


def build_pubg_router(profiles, pubg, fetch_pubg_profile):
    router = Router(name="pubg")

    async def show(message, profile):
        await message.answer(
            profile_text(profile),
            reply_markup=buttons([
                [("Обновить статистику", "pubg:refresh")],
                [("Сменить профиль", "pubg:link"), ("Отключить", "pubg:disconnect")],
            ]),
        )

    async def ask_account(message, state):
        await state.set_state(PubgForm.account)
        await message.answer(
            "Отправь ник PUBG. По умолчанию ищем в Steam. Для другой платформы укажи её перед ником, "
            "например <code>xbox Игрок</code>. Бот сохраняет публичную статистику для личного чата."
        )

    async def load(message, state, user, platform, nickname, existing):
        same = existing and existing.platform == platform and existing.nickname.casefold() == nickname.casefold()
        now = utcnow()
        need_playtime = not same or not existing.playtime_at or now - existing.playtime_at >= PLAYTIME_INTERVAL
        need_rank = not same or not existing.rank_at or now - existing.rank_at >= RANK_INTERVAL
        if not need_playtime and not need_rank:
            await state.set_state(None)
            await show(message, existing)
            return
        attempt = time()
        if attempt - (await state.get_data()).get("pubg_attempt_at", 0) < 15:
            await message.answer("Подожди 15 секунд между запросами статистики PUBG.")
            return
        await state.update_data(pubg_attempt_at=attempt)
        try:
            stats = await fetch_pubg_profile(
                platform, nickname, account_id=existing.account_id if same else None,
                need_playtime=need_playtime, need_rank=need_rank,
            )
            profile = pubg.save(user.id, stats, expected_checked_at=existing.checked_at if existing else None)
        except DomainError as error:
            await message.answer(escape(str(error)))
            if same:
                await show(message, existing)
            return
        await state.set_state(None)
        await show(message, profile)

    async def link(message, state, user, value, existing):
        try:
            platform, nickname = parse_account(value)
        except DomainError as error:
            await message.answer(escape(str(error)))
            return
        await load(message, state, user, platform, nickname, existing)

    @router.message(Command("pubg"))
    async def pubg_command(message, state: FSMContext, command: CommandObject):
        user = await require_profile(profiles, message)
        if not user:
            return
        existing = pubg.get(user.id)
        if command.args:
            await link(message, state, user, command.args, existing)
        elif existing:
            await show(message, existing)
        else:
            await ask_account(message, state)

    @router.message(PubgForm.account, F.text, ~F.text.startswith("/"))
    async def pubg_account(message, state: FSMContext):
        user = await require_profile(profiles, message)
        if user:
            await link(message, state, user, message.text, pubg.get(user.id))

    @router.callback_query(F.data.in_({"pubg:refresh", "pubg:link", "pubg:disconnect"}))
    async def pubg_action(callback, state: FSMContext):
        await callback.answer()
        user = profiles.user_by_telegram(callback.from_user.id)
        if not user or user.banned:
            return
        message = callback.message
        if callback.data == "pubg:disconnect":
            pubg.disconnect(user.id)
            if await state.get_state() == PubgForm.account.state:
                await state.set_state(None)
            await message.answer("Профиль PUBG отключён. Сохранённая статистика удалена.")
            return
        existing = pubg.get(user.id)
        if callback.data == "pubg:link" or not existing:
            await ask_account(message, state)
        else:
            await load(message, state, user, existing.platform, existing.nickname, existing)

    return router
