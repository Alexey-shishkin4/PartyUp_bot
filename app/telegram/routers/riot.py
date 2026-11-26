from datetime import timedelta
from html import escape
from time import time

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext

from app.domain.errors import DomainError
from app.domain.riot import (
    LEAGUE_FLEX,
    LEAGUE_SOLO,
    VALORANT_ACT,
    VALORANT_RANKED_ACTS,
    VALORANT_RIOT_HISTORY,
    RiotGame,
    RiotId,
    normalize_region,
)
from app.persistence.models import utcnow
from app.telegram.states import RiotForm
from app.telegram.ui import buttons, require_profile

GAME_NAMES = {RiotGame.VALORANT: "Valorant", RiotGame.LEAGUE: "League of Legends"}
GAME_FORMS = {RiotGame.VALORANT: RiotForm.valorant, RiotGame.LEAGUE: RiotForm.league}
SCOPE_LABELS = {
    VALORANT_ACT: "Рейтинговые матчи за текущий акт",
    VALORANT_RANKED_ACTS: "Рейтинговые матчи за все доступные акты",
    VALORANT_RIOT_HISTORY: "Матчи во всех режимах по доступной истории Riot",
    LEAGUE_SOLO: "Матчи в Solo/Duo",
    LEAGUE_FLEX: "Матчи в Flex",
}
SOURCE_NAMES = {
    "ugi": "ugiAPI",
    "henrik": "HenrikDev API",
    "henrik_ugi": "HenrikDev API (ранг), ugiAPI (матчи)",
    "ugi_henrik": "ugiAPI (ранг), HenrikDev API (матчи)",
    "opgg": "OP.GG",
    "opgg_cached": "OP.GG",
    "iodid": "Iodid (резерв)",
    "riot": "Riot Games API",
}
REFRESH_INTERVAL = timedelta(minutes=15)
CALLBACKS = {f"riot:{game}:{action}" for game in RiotGame for action in ("refresh", "link", "disconnect")}


def is_fresh(profile):
    return profile.match_scope != VALORANT_ACT and utcnow() - profile.checked_at < REFRESH_INTERVAL


def build_riot_router(profiles, riot, fetch_riot_profile):
    router = Router(name="riot")

    async def show(message, game, profile):
        rank = escape(profile.rank) if profile.rank else "нет данных"
        matches = profile.match_count if profile.match_count is not None else "нет данных"
        if profile.match_scope == VALORANT_ACT:
            scope_note = "Обнови статистику, чтобы получить число матчей за доступную историю.\n"
        elif profile.source == "opgg_cached":
            scope_note = "Снимок OP.GG старше суток или без подтверждённой даты обновления.\n"
        elif profile.source == "iodid":
            scope_note = "Iodid может возвращать сохранённую статистику с задержкой.\n"
        else:
            scope_note = ""
        rank_label = "Ранг по данным источника" if profile.source in {"opgg_cached", "iodid"} else "Текущий ранг"
        checked_label = "запрос бота " if game == RiotGame.LEAGUE else ""
        await message.answer(
            f"<b>{GAME_NAMES[game]} · {escape(profile.game_name)}#{escape(profile.tag_line)}</b>\n"
            f"Сервер: {profile.region.upper()}\n"
            f"{rank_label}: {rank}\n"
            f"{SCOPE_LABELS[profile.match_scope]}: {matches}\n"
            f"{scope_note}"
            f"Источник: {SOURCE_NAMES[profile.source]} · {checked_label}{profile.checked_at:%d.%m.%Y %H:%M} UTC",
            reply_markup=buttons(
                [
                    [("Обновить статистику", f"riot:{game}:refresh")],
                    [("Сменить Riot ID", f"riot:{game}:link"), ("Отключить", f"riot:{game}:disconnect")],
                ]
            ),
        )

    async def ask_account(message, state, game):
        await state.set_state(GAME_FORMS[game])
        await message.answer(
            f"Отправь свой Riot ID для {GAME_NAMES[game]}. Пример — <code>Имя#Тег</code>.\n"
            "Бот передаст Riot ID источнику статистики и сохранит результат для показа в этом чате."
        )

    async def load(message, state, user_id, game, region, account, existing):
        now = time()
        rate_key = f"riot_attempt_at_{game}"
        if now - (await state.get_data()).get(rate_key, 0) < 15:
            await message.answer("Подожди 15 секунд между запросами статистики этой игры.")
            return
        await state.update_data({rate_key: now})
        try:
            stats = await fetch_riot_profile(game, region, account)
            profile = riot.save(
                user_id, game, stats, expected_checked_at=existing.checked_at if existing else None
            )
        except DomainError as error:
            await message.answer(escape(str(error)))
            return
        await state.set_state(None)
        await show(message, game, profile)

    async def link(message, state, user_id, game, text, existing, *, allow_region=False):
        region = None
        if allow_region:
            parts = text.split(maxsplit=1)
            if len(parts) == 2:
                try:
                    region = normalize_region(game, parts[0])
                except DomainError:
                    pass
                else:
                    text = parts[1]
        try:
            account = RiotId.parse(text)
        except DomainError as error:
            await message.answer(escape(str(error)))
            return
        if existing and RiotId(existing.game_name, existing.tag_line).normalized == account.normalized:
            region = region or existing.region
            if region == existing.region and is_fresh(existing):
                await state.set_state(None)
                await show(message, game, existing)
                return
        await load(message, state, user_id, game, region, account, existing)

    @router.message(Command("valorant", "lol"))
    async def riot_command(message, state: FSMContext, command: CommandObject):
        user = await require_profile(profiles, message)
        if not user:
            return
        game = RiotGame(command.command)
        existing = riot.get(user.id, game)
        if command.args:
            await link(message, state, user.id, game, command.args, existing, allow_region=True)
        elif existing:
            await show(message, game, existing)
        else:
            await ask_account(message, state, game)

    @router.message(RiotForm.valorant, F.text, ~F.text.startswith("/"))
    @router.message(RiotForm.league, F.text, ~F.text.startswith("/"))
    async def riot_account(message, state: FSMContext):
        user = await require_profile(profiles, message)
        if user:
            game = (
                RiotGame.VALORANT if await state.get_state() == RiotForm.valorant.state else RiotGame.LEAGUE
            )
            await link(message, state, user.id, game, message.text, riot.get(user.id, game))

    @router.callback_query(F.data.in_(CALLBACKS))
    async def riot_action(callback, state: FSMContext):
        await callback.answer()
        user = profiles.user_by_telegram(callback.from_user.id)
        if not user or user.banned:
            return
        _, game_code, action = callback.data.split(":")
        game = RiotGame(game_code)
        message = callback.message
        if action == "disconnect":
            riot.disconnect(user.id, game)
            if await state.get_state() == GAME_FORMS[game].state:
                await state.set_state(None)
            await message.answer(f"{GAME_NAMES[game]} отключён. Сохранённая статистика удалена.")
            return
        existing = riot.get(user.id, game)
        if action == "link" or existing is None:
            await ask_account(message, state, game)
            return
        if is_fresh(existing):
            await message.answer("Статистика обновляется раз в 15 минут.")
            await show(message, game, existing)
            return
        account = RiotId(existing.game_name, existing.tag_line)
        await load(message, state, user.id, game, existing.region, account, existing)

    return router
