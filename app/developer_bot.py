"""Private Telegram developer panel. Authorization is checked on every input."""

from html import escape

from aiogram import F, Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardRemove

from .developer import DeveloperAccessDenied, DeveloperService
from .service import DomainError


class DeveloperStates(StatesGroup):
    value = State()
    photo = State()
    games = State()


FIELDS = {
    "name": ("Имя", "От 2 до 40 символов"),
    "age": ("Возраст", "Число от 18 до 99"),
    "bio": ("Описание", "До 500 символов; «-» — очистить"),
    "gender": ("Пол", "male, female или other"),
    "looking_for": ("Кого показывать", "any, male, female или other"),
    "purpose": ("Цель", "friends, romance или both"),
    "age_range": ("Возрастной фильтр", "Диапазон, например 20-30"),
    "timezone": ("Часовой пояс", "Например Asia/Novosibirsk"),
    "hours": ("Время игры", "Например 19-23 или 22-2"),
    "language": ("Язык", "ru или en"),
}
CHOICES = {
    "gender": [("Мужской", "male"), ("Женский", "female"), ("Другой", "other")],
    "looking_for": [("Всех", "any"), ("Мужчин", "male"), ("Женщин", "female"), ("Другой", "other")],
    "purpose": [("Друзья", "friends"), ("Отношения", "romance"), ("Оба варианта", "both")],
    "language": [("Русский", "ru"), ("English", "en")],
}


def keyboard(rows):
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=label, callback_data=data) for label, data in row] for row in rows
        ]
    )


def parse_edit(field, value):
    value = value.strip()
    if field == "age":
        try:
            return {"age": int(value)}
        except ValueError:
            raise DomainError("Возраст должен быть числом") from None
    if field in {"age_range", "hours"}:
        try:
            left, right = map(int, value.replace("–", "-").split("-"))
        except ValueError:
            raise DomainError("Укажи диапазон, например 19-23") from None
        return (
            {"age_min": left, "age_max": right}
            if field == "age_range"
            else {"start_hour": left, "end_hour": right}
        )
    if field not in FIELDS:
        raise DomainError("Неизвестное поле")
    return {field: "" if field == "bio" and value == "-" else value}


def build_developer_router(service, settings, send_card, profile_text, main_menu):
    router = Router(name="developer")
    router.message.filter(F.chat.type == "private")
    router.callback_query.filter(F.message.chat.type == "private")
    dev = DeveloperService(service, settings)

    async def authorized(message, state):
        try:
            dev.require(message.from_user.id)
            return True
        except DeveloperAccessDenied as error:
            if (await state.get_state() or "").startswith("DeveloperStates:"):
                await state.clear()
            await message.answer(str(error))
            return False

    async def panel(message, actor):
        info = dev.stats(actor)
        await message.answer(
            "🛠 <b>Режим разработчика</b>\n"
            f"Анкет: {info['profiles']} · реальных: {info['real']} · тестовых: {info['test']}\n"
            f"Активных: {info['active']} · пауза: {info['paused']} · бан: {info['banned']}\n"
            f"Совпадений: {info['matches']} · открытых жалоб: {info['reports']}\n\n"
            "Выбери анкету или инструмент проверки. /dev ID — открыть анкету напрямую.",
            reply_markup=keyboard(
                [
                    [("Все анкеты", "dev:list:all:0"), ("Реальные", "dev:list:real:0")],
                    [("Тестовые", "dev:list:test:0"), ("Скрытые / пауза", "dev:list:paused:0")],
                    [("Заблокированные", "dev:list:banned:0"), ("Открыть по ID", "dev:input:open")],
                    [("Предпросмотр поиска", "dev:input:preview"), ("Сравнить анкеты", "dev:input:compare")],
                    [("Диагностика", "dev:diagnostics"), ("Журнал изменений", "dev:audit")],
                    [("Сбросить мои тестовые просмотры", "dev:reset_tests")],
                    [("Выйти в обычное меню", "dev:exit")],
                ]
            ),
        )

    async def listing(message, actor, scope, offset):
        page = dev.list_profiles(actor, offset, scope)
        rows = []
        for profile in page["profiles"]:
            badge = "🧪" if profile["is_test"] else "👤"
            status = " ⛔" if profile["banned"] else " ⏸" if not profile["active"] else ""
            rows.append(
                [
                    (
                        f"{badge} #{profile['id']} {profile['name']} · {profile['age']}{status}",
                        f"dev:profile:{profile['id']}",
                    )
                ]
            )
        navigation = []
        if page["offset"] > 0:
            navigation.append(("← Назад", f"dev:list:{scope}:{max(0, offset - 5)}"))
        if page["offset"] + 5 < page["total"]:
            navigation.append(("Дальше →", f"dev:list:{scope}:{offset + 5}"))
        if navigation:
            rows.append(navigation)
        rows.append([("Панель разработчика", "dev:home")])
        await message.answer(
            f"🛠 Анкеты: {page['total']} · фильтр: {escape(scope)}\n"
            "Открой карточку для просмотра и редактирования.",
            reply_markup=keyboard(rows),
        )

    async def card(message, actor, target):
        profile = dev.read_profile(actor, target)
        status = "заблокирована" if profile["banned"] else "активна" if profile["active"] else "на паузе"
        username = "@" + profile["username"] if profile["username"] else "не указан"
        text = (
            f"🛠 <b>Анкета #{target}</b> · {status}\n"
            f"Telegram ID: <code>{profile['telegram_id']}</code> · {escape(username)}\n"
            f"Steam ID: {escape(profile['steam_id'] or 'не подключён')}\n"
            f"Фильтр возраста: {profile['age_min']}–{profile['age_max']} · "
            f"пол: {profile['gender']} · ищет: {profile['looking_for']}\n\n" + profile_text(profile)
        )
        rows = [
            [("Редактировать поля", f"dev:fields:{target}"), ("Фото", f"dev:photo:{target}")],
            [("Игры", f"dev:games:{target}"), ("Проверить подбор", f"dev:preview:{target}")],
            [("Вид карточки", f"dev:presentation:{target}")],
            [
                (("На паузу" if profile["active"] else "Включить показы"), f"dev:toggle:active:{target}"),
                (("Снять бан" if profile["banned"] else "Заблокировать"), f"dev:toggle:banned:{target}"),
            ],
            [("Все анкеты", "dev:list:all:0"), ("Панель", "dev:home")],
        ]
        await send_card(message, profile, text, reply_markup=keyboard(rows))

    async def presentation(message, actor, target):
        profile = dev.read_profile(actor, target)
        context = "🛠 Закрытый предпросмотр интерфейса для разработчиков."
        if profile["is_test"]:
            context += "\nДанные анкеты вымышлены; фото — иллюстрация."
        await message.answer(context)
        await send_card(
            message,
            profile,
            reply_markup=keyboard([[("К анкете", f"dev:profile:{target}"), ("Панель", "dev:home")]]),
            developer_preview=True,
        )

    async def fields(message, actor, target):
        dev.read_profile(actor, target)
        items = [(title, f"dev:field:{target}:{field}") for field, (title, _) in FIELDS.items()]
        rows = [items[index : index + 2] for index in range(0, len(items), 2)]
        rows.append([("Назад к анкете", f"dev:profile:{target}")])
        await message.answer(f"🛠 Редактирование анкеты #{target}. Выбери поле.", reply_markup=keyboard(rows))

    async def games(message, state, actor, query=""):
        data = await state.get_data()
        target = data["target"]
        dev.read_profile(actor, target)
        selected = data["selected"]
        found = [
            game
            for game in service.db.catalog()
            if not query or any(query.casefold() in name.casefold() for name in [game.name, *game.aliases])
        ][:12]
        rows = [
            [(f"{'✓ ' if game.app_id in selected else ''}{game.name}", f"dev:game:{target}:{game.app_id}")]
            for game in found
        ]
        rows.extend([[("Сохранить игры", f"dev:games_save:{target}")], [("Отмена", f"dev:profile:{target}")]])
        await message.answer(
            f"🛠 Игры анкеты #{target}: выбрано {len(selected)}/10.\n"
            "Отправь название для поиска или выбирай кнопками.",
            reply_markup=keyboard(rows),
        )

    async def preview(message, actor, target):
        rows = dev.preview(actor, target)
        text = f"🧪 Предпросмотр поиска от анкеты #{target}. Показов, лайков и сообщений от её имени не создаётся.\n"
        if rows:
            text += "\n".join(
                f"#{row['profile']['id']} {row['profile']['name']} · {row['score']}/100"
                + (" · тестовая" if row["profile"]["is_test"] else "")
                for row in rows
            )
        else:
            text += "Подходящих новых анкет нет: проверь паузу, бан, предпочтения и прежние реакции."
        await message.answer(
            escape(text),
            reply_markup=keyboard([[("К анкете", f"dev:profile:{target}"), ("Панель", "dev:home")]]),
        )

    async def apply_value(message, state, actor, value):
        data = await state.get_data()
        operation = data.get("operation")
        if operation == "edit":
            target = data["target"]
            dev.update_profile(actor, target, parse_edit(data["field"], value))
            await state.clear()
            await card(message, actor, target)
        elif operation in {"open", "preview"}:
            try:
                target = int(value.strip())
            except ValueError:
                raise DomainError("Нужен числовой ID анкеты") from None
            if operation == "open":
                await card(message, actor, target)
            else:
                await preview(message, actor, target)
            await state.clear()
        elif operation == "compare":
            try:
                left, right = map(int, value.replace(",", " ").split())
            except ValueError:
                raise DomainError("Укажи два ID, например 1 2") from None
            result = dev.compare(actor, left, right)
            parts = result["components"]
            await message.answer(
                f"🧪 #{left} ↔ #{right}\nФильтры: {'подходят' if result['compatible'] else 'не подходят'}\n"
                f"Сходство: {result['score']}/100\nИгры: {parts['games']} · "
                f"жанры: {parts['genres']} · время: {parts['time']}\n"
                f"Общие игры: {escape(', '.join(result['common_games'])) or 'нет'}",
                reply_markup=keyboard([[("Панель", "dev:home")]]),
            )
            await state.clear()
        else:
            raise DomainError("Сессия редактирования устарела. Открой /dev заново")

    @router.message(Command("id", "whoami"))
    async def whoami(message):
        await message.answer(
            f"Твой Telegram ID: <code>{message.from_user.id}</code>"
            + (f"\n@{escape(message.from_user.username)}" if message.from_user.username else "")
        )

    @router.message(Command("dev"))
    async def open_panel(message, state: FSMContext):
        if not await authorized(message, state):
            return
        await state.clear()
        await message.answer(
            "🛠 Режим разработчика. /cancel — отменить действие.", reply_markup=ReplyKeyboardRemove()
        )
        parts = message.text.split()
        try:
            if len(parts) == 2:
                await card(message, message.from_user.id, int(parts[1]))
            else:
                await panel(message, message.from_user.id)
        except (DomainError, ValueError) as error:
            await message.answer(escape(str(error)))

    @router.message(
        StateFilter(DeveloperStates.value, DeveloperStates.photo, DeveloperStates.games), Command("cancel")
    )
    async def cancel(message, state: FSMContext):
        if not await authorized(message, state):
            return
        await state.clear()
        await panel(message, message.from_user.id)

    @router.callback_query(F.data.startswith("dev:"))
    async def callback(callback, state: FSMContext):
        actor = callback.from_user.id
        try:
            dev.require(actor)
        except DeveloperAccessDenied as error:
            if (await state.get_state() or "").startswith("DeveloperStates:"):
                await state.clear()
            await callback.answer(str(error), show_alert=True)
            return
        await callback.answer()
        parts = callback.data.split(":")
        action = parts[1]
        message = callback.message
        try:
            if action in {
                "home",
                "exit",
                "list",
                "profile",
                "fields",
                "diagnostics",
                "audit",
                "input",
                "preview",
                "presentation",
                "toggle",
                "reset_tests",
            }:
                await state.clear()
            if action == "home":
                await panel(message, actor)
            elif action == "exit":
                await message.answer("Обычное меню.", reply_markup=main_menu())
            elif action == "list":
                await listing(message, actor, parts[2], int(parts[3]))
            elif action == "profile":
                await card(message, actor, int(parts[2]))
            elif action == "fields":
                await fields(message, actor, int(parts[2]))
            elif action == "field":
                target, field = int(parts[2]), parts[3]
                dev.read_profile(actor, target)
                if field not in FIELDS:
                    raise DomainError("Неизвестное поле")
                await state.set_state(DeveloperStates.value)
                await state.set_data({"operation": "edit", "target": target, "field": field})
                rows = [
                    [(label, f"dev:pick:{target}:{field}:{value}")] for label, value in CHOICES.get(field, [])
                ]
                rows.append([("Отмена", f"dev:profile:{target}")])
                await message.answer(
                    f"Анкета #{target} · {FIELDS[field][0]}\n{FIELDS[field][1]}\nОтправь новое значение.",
                    reply_markup=keyboard(rows),
                )
            elif action == "pick":
                data = await state.get_data()
                target, field = int(parts[2]), parts[3]
                if data.get("target") != target or data.get("field") != field:
                    raise DomainError("Кнопка устарела")
                await apply_value(message, state, actor, parts[4])
            elif action == "toggle":
                field, target = parts[2], int(parts[3])
                if field not in {"active", "banned"}:
                    raise DomainError("Неизвестный статус")
                current = dev.read_profile(actor, target)
                dev.update_profile(actor, target, {field: not current[field]})
                await card(message, actor, target)
            elif action == "photo":
                target = int(parts[2])
                dev.read_profile(actor, target)
                await state.set_state(DeveloperStates.photo)
                await state.set_data({"target": target})
                await message.answer(
                    f"Отправь обычное фото для анкеты #{target}.",
                    reply_markup=keyboard(
                        [
                            [("Удалить фото", f"dev:photo_remove:{target}")],
                            [("Отмена", f"dev:profile:{target}")],
                        ]
                    ),
                )
            elif action == "photo_remove":
                target = int(parts[2])
                data = await state.get_data()
                if data.get("target") != target or await state.get_state() != DeveloperStates.photo.state:
                    raise DomainError("Кнопка устарела")
                dev.set_photo(actor, target, None)
                await state.clear()
                await card(message, actor, target)
            elif action == "games":
                target = int(parts[2])
                profile = dev.read_profile(actor, target)
                await state.set_state(DeveloperStates.games)
                await state.set_data({"target": target, "selected": profile["game_ids"]})
                await games(message, state, actor)
            elif action in {"game", "games_save"}:
                target = int(parts[2])
                data = await state.get_data()
                if data.get("target") != target or await state.get_state() != DeveloperStates.games.state:
                    raise DomainError("Кнопка устарела")
                if action == "games_save":
                    dev.set_games(actor, target, data["selected"])
                    await state.clear()
                    await card(message, actor, target)
                else:
                    app_id = int(parts[3])
                    selected = list(data["selected"])
                    if app_id in selected:
                        selected.remove(app_id)
                    elif len(selected) < 10:
                        selected.append(app_id)
                    await state.update_data(selected=selected)
                    await games(message, state, actor)
            elif action == "input":
                operation = parts[2]
                if operation not in {"open", "preview", "compare"}:
                    raise DomainError("Неизвестный инструмент")
                await state.set_state(DeveloperStates.value)
                await state.set_data({"operation": operation})
                await message.answer(
                    "Укажи два ID анкет через пробел." if operation == "compare" else "Укажи ID анкеты."
                )
            elif action == "preview":
                await preview(message, actor, int(parts[2]))
            elif action == "presentation":
                await presentation(message, actor, int(parts[2]))
            elif action == "reset_tests":
                dev.reset_tests(actor)
                await message.answer("Твои тестовые просмотры сброшены.")
                await panel(message, actor)
            elif action == "diagnostics":
                info = dev.stats(actor)
                await message.answer(
                    "🧪 Диагностика\n" + "\n".join(f"{key}: {value}" for key, value in info.items()),
                    reply_markup=keyboard([[("Панель", "dev:home")]]),
                )
            elif action == "audit":
                entries = dev.history(actor)
                text = "🧾 Последние изменения\n" + "\n".join(
                    f"#{row.id} · анкета {row.target_user_id} · {row.action} · разработчик {row.actor_telegram_id}"
                    for row in entries
                )
                await message.answer(
                    escape(text if entries else "Изменений разработчиков пока нет."),
                    reply_markup=keyboard([[("Панель", "dev:home")]]),
                )
            else:
                raise DomainError("Неизвестная кнопка")
        except (DomainError, ValueError, IndexError) as error:
            await message.answer(
                escape(
                    str(error) if isinstance(error, DomainError) else "Неверная команда. Открой /dev заново"
                )
            )

    @router.message(DeveloperStates.value, F.text, ~F.text.startswith("/"))
    async def value(message, state: FSMContext):
        if not await authorized(message, state):
            return
        try:
            await apply_value(message, state, message.from_user.id, message.text)
        except DomainError as error:
            await message.answer(escape(str(error)))

    @router.message(DeveloperStates.photo, F.photo)
    async def photo(message, state: FSMContext):
        if not await authorized(message, state):
            return
        data = await state.get_data()
        image = max(message.photo, key=lambda row: row.width * row.height)
        if image.file_size and image.file_size > 10 * 1024 * 1024:
            await message.answer("Отправь фото до 10 МБ.")
            return
        try:
            dev.set_photo(message.from_user.id, data["target"], image.file_id)
            await state.clear()
            await card(message, message.from_user.id, data["target"])
        except DomainError as error:
            await message.answer(escape(str(error)))

    @router.message(DeveloperStates.photo, ~F.text.startswith("/"))
    async def unsupported_photo(message, state: FSMContext):
        if not await authorized(message, state):
            return
        await message.answer("Отправь обычную фотографию через скрепку или /cancel.")

    @router.message(DeveloperStates.games, F.text, ~F.text.startswith("/"))
    async def search_games(message, state: FSMContext):
        if not await authorized(message, state):
            return
        try:
            await games(message, state, message.from_user.id, message.text.strip())
        except DomainError as error:
            await message.answer(escape(str(error)))

    return router
