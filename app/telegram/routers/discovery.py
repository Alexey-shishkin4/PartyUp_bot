import logging
from html import escape

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext

from app.domain.errors import DomainError
from app.services.fixture_catalog import fixture_definitions
from app.telegram.cards import profile_text
from app.telegram.contacts import current_username as resolve_username
from app.telegram.states import Form
from app.telegram.ui import USERNAME_REQUIRED, buttons, clear_buttons, menu, require_profile

log = logging.getLogger(__name__)


def build_discovery_router(profiles, discovery, cards, feed_view):
    router = Router(name="discovery")

    async def peer_contact(bot, peer):
        try:
            username = await resolve_username(bot, peer)
        except (TelegramAPIError, TimeoutError):
            return "Контакт пока недоступен. Попробуй обновить список совпадений позже."
        if not username:
            return "У собеседника пока нет @username."
        return f'Написать в Telegram: <a href="https://t.me/{username}">@{username}</a>'

    async def show_matches(message, user):
        rows = discovery.matches(user.id)
        if not rows:
            await message.answer("Здесь появятся взаимные симпатии 💜", reply_markup=menu())
        for row in rows[:30]:
            try:
                peer = discovery.match_peer(user.id, row["id"])
                contact = await peer_contact(message.bot, peer)
                discovery.match_peer(user.id, row["id"])
            except DomainError:
                continue
            await cards.send(
                message,
                row["profile"],
                profile_text(row["profile"]) + "\n\n" + contact,
                reply_markup=buttons(
                    [
                        [
                            ("Заблокировать в боте", f"match:block:{row['id']}"),
                            ("Пожаловаться", f"match:report:{row['id']}"),
                        ],
                        [("Мы пообщались / сыграли", f"feedback:{row['id']}")],
                    ]
                ),
            )

    async def show_test_feed(message, user):
        rows = discovery.feed(user.id, limit=1, test_only=True)
        if not rows:
            await message.answer(
                "Тестовые анкеты просмотрены.",
                reply_markup=buttons([[("Посмотреть сначала", "test:restart")]]),
            )
            return
        row = rows[0]
        text = profile_text(row["profile"], common_games=row["common_games"])
        text += f"\n\nСходство игровых интересов: {row['score']}/100."
        target = row["profile"]["id"]
        await cards.send(
            message,
            row["profile"],
            text,
            reply_markup=buttons(
                [
                    [
                        ("💜 Тестовый лайк", f"testreact:like:{target}"),
                        ("Дальше →", f"testreact:skip:{target}"),
                    ],
                    [("Начать сначала", "test:restart")],
                ]
            ),
        )

    @router.message(Command("find"))
    @router.message(F.text == "🔎 Найти своих")
    async def find(message, state: FSMContext):
        await state.clear()
        user = await require_profile(profiles, message)
        if user:
            await feed_view.show(message, user)

    @router.message(Command("matches"))
    @router.message(F.text == "💜 Совпадения")
    async def matches(message, state: FSMContext):
        await state.clear()
        user = await require_profile(profiles, message)
        if user:
            user = profiles.update_telegram_account(message.from_user.id, message.from_user.username)
            await show_matches(message, user)

    @router.callback_query(F.data.startswith("match:"))
    async def match_action(callback, state: FSMContext):
        await callback.answer()
        user = profiles.user_by_telegram(callback.from_user.id)
        if not user or user.banned:
            return
        _, action, *args = callback.data.split(":")
        if action in {"enable", "list"}:
            user = profiles.update_telegram_account(callback.from_user.id, callback.from_user.username)
            await show_matches(callback.message, user)
        elif action in {"block", "report"}:
            try:
                peer = discovery.match_peer(user.id, int(args[0]))
                discovery.react(user.id, peer.id, action)
            except (DomainError, IndexError, ValueError) as error:
                await callback.message.answer(escape(str(error)))
                return
            await clear_buttons(callback.message)
            text = (
                "Контакт заблокирован в боте."
                if action == "block"
                else "Жалоба отправлена. Контакт заблокирован в боте."
            )
            await callback.message.answer(
                text + " Если вы уже переписываетесь в Telegram, заблокируй собеседника и там."
            )

    async def show_focus(message, query=""):
        catalog = profiles.catalog()
        needle = query.casefold()
        games = [
            game for game in catalog if any(needle in name.casefold() for name in [game.name, *game.aliases])
        ][:12]
        rows = [[(game.name[:55], f"focus:game:{game.app_id}")] for game in games]
        rows.append([("Общий поиск", "focus:clear")])
        text = "В какую игру хочешь сыграть? Выбор действует четыре часа. Можешь написать название."
        if not games:
            text = "Игра не найдена. Попробуй другое название."
        await message.answer(text, reply_markup=buttons(rows))

    @router.message(Command("play"))
    @router.message(F.text == "🎮 Хочу сыграть сейчас")
    async def focus_menu(message, state: FSMContext):
        if await require_profile(profiles, message):
            await state.clear()
            await state.set_state(Form.focus)
            await show_focus(message)

    @router.message(Command("alerts"))
    async def alerts(message):
        user = await require_profile(profiles, message)
        if user:
            text = (
                "Уведомление о новой подходящей анкете включено."
                if user.notify_new_profiles
                else "Уведомление о новых анкетах выключено."
            )
            rows = [[("Отключить", "alert:off")]] if user.notify_new_profiles else []
            await message.answer(text, reply_markup=buttons(rows) if rows else None)

    @router.callback_query(F.data == "alert:off")
    async def disable_alert(callback):
        user = profiles.user_by_telegram(callback.from_user.id)
        if user:
            discovery.set_profile_alert(user.id, False)
        await callback.answer("Уведомление отключено")
        await clear_buttons(callback.message)

    @router.callback_query(F.data.startswith("focus:") | F.data.startswith("feed:"))
    async def discovery_action(callback, state: FSMContext):
        user = profiles.user_by_telegram(callback.from_user.id)
        if not user or user.banned:
            await callback.answer("Анкета недоступна.")
            return
        scope, action, *args = callback.data.split(":")
        message = callback.message
        if action == "undo":
            target = discovery.undo_skip(user.id)
            if target is None:
                await callback.answer("Можно вернуть только последний пропуск.", show_alert=True)
                return
            await callback.answer("Пропуск отменён")
            if discovery.feed(user.id, limit=1, record_impressions=False, preferred_id=target):
                await feed_view.show(message, user, replace=True, preferred_id=target)
            else:
                await message.answer("Анкета больше не подходит под текущий поиск или недоступна.")
                await feed_view.show(message, user, replace=True)
            return
        await callback.answer()
        if scope == "focus":
            if action == "game" and await state.get_state() != Form.focus.state:
                return
            if action not in {"game", "clear"}:
                return
            discovery.set_current_game(user.id, int(args[0]) if action == "game" else None)
            await state.clear()
            await feed_view.show(message, profiles.user(user.id), replace=True)
        elif action == "focus":
            await state.clear()
            await state.set_state(Form.focus)
            await show_focus(message)
        elif action in {"alert_on", "alert_off"}:
            enabled = action == "alert_on"
            discovery.set_profile_alert(user.id, enabled)
            await message.answer(
                "Сообщу один раз, когда появится подходящая анкета." if enabled else "Уведомление отключено."
            )
            await feed_view.show(message, profiles.user(user.id), replace=True)
        elif action == "resume":
            if not user.active:
                profiles.toggle_active(user.id)
            await feed_view.show(message, profiles.user(user.id), replace=True)

    @router.message(Command("test"))
    async def test_profiles(message, state: FSMContext):
        if not discovery.include_test_profiles:
            await message.answer("Тестовая лента выключена.")
            return
        user = await require_profile(profiles, message)
        if not user:
            return
        await state.clear()
        await message.answer(
            f"🧪 Тестовых анкет: {len(fixture_definitions())}. "
            "Это примеры для проверки карточек и кнопок. В тестовом просмотре "
            "возрастные фильтры и предпочтения не применяются. /find — обычный поиск.",
            reply_markup=menu(),
        )
        await show_test_feed(message, user)

    @router.callback_query(F.data == "test:restart")
    async def restart_test_profiles(callback, state: FSMContext):
        if not discovery.include_test_profiles:
            await callback.answer("Тестовая лента выключена.")
            return
        await callback.answer()
        user = profiles.user_by_telegram(callback.from_user.id)
        if not user or user.banned:
            return
        await state.clear()
        discovery.reset_test_reactions(user.id)
        await callback.message.edit_reply_markup(reply_markup=None)
        await show_test_feed(callback.message, user)

    @router.callback_query(F.data.regexp(r"^(?:react|testreact):(like|skip|block|report):\d+$"))
    async def react(callback):
        message = callback.message
        await callback.answer()
        user = profiles.update_telegram_account(callback.from_user.id, callback.from_user.username)
        if not user or user.banned:
            return
        origin, kind, target = callback.data.split(":")
        peer = profiles.user(int(target))
        if not peer or (origin == "testreact" and not peer.is_test):
            await callback.message.answer("Анкета больше недоступна.")
            return
        if kind == "like" and not peer.is_test and not user.username:
            await message.answer(USERNAME_REQUIRED)
            return
        try:
            result = discovery.react(user.id, peer.id, kind, fresh=True, gallery=origin == "testreact")
        except DomainError as error:
            await callback.message.answer(escape(str(error)))
            return
        if origin == "testreact":
            await clear_buttons(callback.message)
            if kind == "like":
                await callback.message.answer("Тестовый лайк сохранён ✓")
            await show_test_feed(callback.message, profiles.user(user.id))
            return
        if kind == "report":
            await callback.message.answer(
                "Жалоба отправлена в очередь модерации. Анкета также заблокирована для тебя."
            )
        if result["matched"] and result["new"]:
            mine = await peer_contact(message.bot, user)
            theirs = await peer_contact(message.bot, peer)
            try:
                discovery.match_peer(user.id, result["match_id"])
            except DomainError:
                await feed_view.show(callback.message, profiles.user(user.id), replace=True)
                return
            markup = buttons([[("Открыть совпадения", "match:list")]])
            hint = (
                "" if callback.from_user.username else "\nДля обмена добавь @username в настройках Telegram."
            )
            await callback.message.answer(
                f"💜 Взаимная симпатия с {escape(peer.name)}!\n{theirs}" + hint, reply_markup=markup
            )
            try:
                discovery.match_peer(peer.id, result["match_id"])
                await message.bot.send_message(
                    peer.telegram_id,
                    f"💜 Взаимная симпатия с {escape(user.name)}!\n{mine}",
                    reply_markup=markup,
                )
            except DomainError:
                pass
            except TelegramAPIError as error:
                log.info("Could not deliver a match notification: %s", type(error).__name__)
        await feed_view.show(
            callback.message, profiles.user(user.id), replace=True, allow_undo=kind == "skip"
        )

    @router.callback_query(F.data.startswith("chat:") | F.data.startswith("contact:"))
    async def old_chat_button(callback, state: FSMContext):
        await callback.answer()
        await state.clear()
        user = profiles.update_telegram_account(callback.from_user.id, callback.from_user.username)
        if user and not user.banned:
            await show_matches(callback.message, user)

    @router.callback_query(F.data.startswith("feedback:"))
    async def feedback(callback):
        await callback.answer()
        user = profiles.user_by_telegram(callback.from_user.id)
        if user:
            try:
                peer = discovery.match_peer(user.id, int(callback.data.split(":")[1]))
                discovery.record(user.id, peer.id, "positive_feedback")
                await callback.message.answer("Спасибо! Учтём, что знакомство состоялось.")
            except DomainError:
                await callback.message.answer("Совпадение недоступно.")

    @router.message(Form.focus, F.text, ~F.text.startswith("/"))
    async def focus_search(message, state: FSMContext):
        await show_focus(message, message.text.strip())

    return router
