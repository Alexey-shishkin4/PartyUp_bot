from html import escape
from time import time

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext

from app.domain.errors import DomainError
from app.domain.steam import PUBG_APP_ID
from app.telegram.states import GameLink
from app.telegram.ui import buttons, clear_buttons, menu, require_profile

NAMES = {
    "pubg": "PUBG",
    "riot": "Riot · League of Legends и Valorant",
    "fortnite": "Fortnite",
    "faceit": "FACEIT · Counter-Strike 2",
    "deadlock": "Deadlock",
}
PROMPTS = {
    "pubg": "Пришли ник или Account ID PUBG. По умолчанию ищем в Steam. Для другой платформы укажи её "
            "перед ником или ID, например <code>xbox Игрок</code>.",
    "riot": "Пришли один Riot ID в формате <code>Имя#Тег</code>. По нему проверим League of Legends и Valorant.",
    "fortnite": "Пришли отображаемое имя Epic Games.",
    "faceit": "Пришли никнейм FACEIT, ссылку на профиль FACEIT или SteamID64.",
}


class GameAccountFlow:
    def __init__(self, profiles, accounts):
        self.profiles = profiles
        self.accounts = accounts

    async def steam_attached(self, message, state, user, *, guide=True, skip=()):
        missing = await self.accounts.sync_steam(user, skip=skip)
        if missing:
            await message.answer("Пока не удалось обновить " + ", ".join(missing) + ". Можно повторить через «Добавить игры».")
        if guide:
            await self.begin(message, state, user)

    async def begin(self, message, state, user):
        if not self.accounts.fetchers:
            return
        linked = self.accounts.connected(user)
        if "pubg" in self.accounts.fetchers and "pubg" not in linked and self.accounts.played(user, PUBG_APP_ID):
            await self.ask(message, state, "pubg", after="riot")
        else:
            await self.offer_riot(message, state, user, linked)

    async def offer_riot(self, message, state, user, linked=None):
        if linked is None:
            linked = self.accounts.connected(user)
        if "riot" not in self.accounts.fetchers or "riot" in linked:
            await self.show_add(message, state, user)
            return
        await state.set_state(GameLink.riot_offer)
        await message.answer(
            "Подключить Riot? По одному Riot ID бот добавит League of Legends и Valorant.",
            reply_markup=buttons([
                [("Подключить Riot", "accounts:riot_yes"), ("Пропустить", "accounts:riot_skip")],
                [("Пропустить всё", "accounts:done")],
            ]),
        )

    async def ask(self, message, state, name, *, after="add"):
        await state.update_data(account_name=name, account_after=after)
        await state.set_state(GameLink.identity)
        rows = [[("Пропустить", f"accounts:skip:{name}")]]
        if after == "riot":
            rows[0].append(("Пропустить всё", "accounts:done"))
        await message.answer(PROMPTS[name], reply_markup=buttons(rows))

    async def show_add(self, message, state, user):
        await state.set_state(None)
        linked = self.accounts.connected(user)
        rows = [] if user.steam_id else [[("Подключить Steam", "steam:open")]]
        rows += [
            [(label, f"accounts:choose:{name}")]
            for name, label in NAMES.items()
            if name in self.accounts.fetchers and name not in linked and (name != "deadlock" or user.steam_id)
        ]
        prompt = "Выбери игру или аккаунт, который хочешь добавить." if rows else "Все доступные аккаунты подключены."
        if linked or user.steam_id:
            rows.append([("Управлять привязками", "accounts:manage")])
        rows.append([("Выбрать игры вручную", "edit:games"), ("Пропустить", "accounts:done")])
        await message.answer("Добавить игры\n\n" + prompt, reply_markup=buttons(rows))

    async def show_manage(self, message, state, user):
        await state.set_state(None)
        rows = [[("Steam", "steam:open")]] if user.steam_id else []
        rows += [[(NAMES[name], f"accounts:manage:{name}")] for name in self.accounts.connected(user)]
        rows.append([("← Добавить игры", "accounts:open")])
        await message.answer("Подключённые аккаунты", reply_markup=buttons(rows))

    async def show_account(self, message, state, name):
        await state.set_state(None)
        rows = [[("Обновить", f"accounts:refresh:{name}"), ("Отключить", f"accounts:disconnect:{name}")]]
        if name in PROMPTS:
            rows.append([("Сменить аккаунт", f"accounts:change:{name}")])
        rows.append([("← Все привязки", "accounts:manage")])
        await message.answer(NAMES[name], reply_markup=buttons(rows))

    async def advance(self, message, state, user, after):
        if after == "riot":
            await self.offer_riot(message, state, user)
        elif after == "manage":
            await self.show_manage(message, state, user)
        else:
            await self.show_add(message, state, user)

    @staticmethod
    async def attempt(message, state, name):
        now = time()
        rate_key = f"account_attempt_{name}"
        if now - (await state.get_data()).get(rate_key, 0) < 15:
            await message.answer("Подожди несколько секунд перед повторной проверкой.")
            return False
        await state.update_data({rate_key: now})
        return True

    def build_router(self):
        router = Router(name="game_accounts")

        @router.message(Command("games"))
        @router.message(F.text == "➕ Добавить игры")
        async def games_menu(message, state: FSMContext):
            user = await require_profile(self.profiles, message)
            if user:
                await self.show_add(message, state, user)

        @router.message(GameLink.identity, F.text, ~F.text.startswith("/"))
        async def identity(message, state: FSMContext):
            user = await require_profile(self.profiles, message)
            if not user:
                return
            data = await state.get_data()
            name = data.get("account_name")
            if name not in self.accounts.fetchers or name not in PROMPTS:
                await self.show_add(message, state, user)
                return
            try:
                lookup = self.accounts.parse_identity(name, message.text)
                if not await self.attempt(message, state, name):
                    return
                missing = await self.accounts.link(user, name, lookup)
            except DomainError as error:
                await message.answer(escape(str(error)), reply_markup=buttons([
                    [("Пропустить", f"accounts:skip:{name}")],
                ]))
                return
            status = "Игровые данные добавлены в анкету."
            if missing:
                status += " Пока не удалось получить данные для " + ", ".join(missing) + "."
            await message.answer(status, reply_markup=buttons([[("Моя анкета", "edit:profile")]]))
            await self.advance(message, state, user, data.get("account_after"))

        @router.callback_query(F.data.startswith("accounts:"))
        async def account_action(callback, state: FSMContext):
            await callback.answer()
            user = self.profiles.user_by_telegram(callback.from_user.id)
            if not user or user.banned:
                return
            message = callback.message
            _, action, *parts = callback.data.split(":")
            name = parts[0] if parts else None
            current = await state.get_state()
            if action in {"riot_yes", "riot_skip"}:
                if current != GameLink.riot_offer.state:
                    return
                await clear_buttons(message)
                if action == "riot_yes":
                    await self.ask(message, state, "riot")
                else:
                    await self.show_add(message, state, user)
            elif action == "skip":
                data = await state.get_data()
                if current == GameLink.identity.state and name == data.get("account_name"):
                    await clear_buttons(message)
                    await self.advance(message, state, user, data.get("account_after"))
            elif action == "done":
                await state.clear()
                await clear_buttons(message)
                await message.answer("Добавить игры можно позже через меню.", reply_markup=menu())
            elif action == "open":
                await self.show_add(message, state, user)
            elif action == "manage" and name is None:
                await self.show_manage(message, state, user)
            elif name in self.accounts.fetchers:
                await self.selected_action(message, state, user, action, name)

        return router

    async def selected_action(self, message, state, user, action, name):
        row = self.accounts.connected(user).get(name)
        if action == "choose" and not row and name in PROMPTS:
            await self.ask(message, state, name)
        elif action == "change" and row and name in PROMPTS:
            await self.ask(message, state, name, after="manage")
        elif action == "manage" and row:
            await self.show_account(message, state, name)
        elif action == "disconnect" and row:
            self.accounts.disconnect(user, name)
            await self.show_manage(message, state, user)
        elif (action == "refresh" and row) or (action == "choose" and name == "deadlock" and user.steam_id):
            if not await self.attempt(message, state, name):
                return
            try:
                missing = await self.accounts.refresh(user, name, row)
            except DomainError as error:
                await message.answer(escape(str(error)))
                return
            text = "Игровые данные в анкете проверены."
            if missing:
                text += " Пока не удалось получить данные для " + ", ".join(missing) + "."
            await message.answer(text, reply_markup=buttons([[("Моя анкета", "edit:profile")]]))
            if row:
                await self.show_account(message, state, name)
            else:
                await self.show_add(message, state, user)
