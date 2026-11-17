import logging
import math
import re
from datetime import datetime, timedelta
from html import escape

from aiogram import Bot, Dispatcher, F, Router
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import SimpleEventIsolation
from aiogram.types import BotCommand, BotCommandScopeChat, FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, Message, ReplyKeyboardMarkup
from pydantic import ValidationError
from sqlalchemy import select

from .models import Event, Report, User, utcnow
from .config import ROOT
from .schemas import ProfileInput
from .service import DomainError, PHOTO_UNCHANGED
from .storage import DatabaseStorage
from .test_profiles import fixture_definitions

log = logging.getLogger(__name__)


class Form(StatesGroup):
    consent = State()
    step = State()
    games = State()
    chat = State()
    steam_link = State()
    photo = State()


def buttons(rows):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=text, callback_data=data) for text, data in row] for row in rows
    ])


def menu():
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="🔎 Найти своих"), KeyboardButton(text="💜 Совпадения")],
        [KeyboardButton(text="👤 Моя анкета"), KeyboardButton(text="🎮 Steam")],
        [KeyboardButton(text="📷 Фото анкеты")],
    ], resize_keyboard=True)


STEPS = [
    ("name", "Как тебя показывать? Имя или ник, от 2 до 40 символов.", None),
    ("age", "Сколько тебе лет? Бот для совершеннолетних: 18–99.", None),
    ("gender", "Как указать твой пол?", [("Мужской", "male"), ("Женский", "female"), ("Другой / не указывать", "other")]),
    ("looking_for", "Кого показывать в общей ленте? Одна настройка для всех знакомств.", [("Всех", "any"), ("Мужчин", "male"), ("Женщин", "female"), ("Другой / не указан", "other")]),
    ("purpose", "Чего ждёшь от знакомства?", [("Друзей и тиммейтов", "friends"), ("Отношений", "romance"), ("Обоих вариантов", "both")]),
    ("age_range", "Желаемый возраст собеседника, например 20-30. Можно 18-99.", None),
    ("language", "На каком языке хочешь общаться?", [("Русский", "ru"), ("English", "en")]),
    ("timezone", "Твой часовой пояс. Можно выбрать или написать, например Asia/Novosibirsk.", [("Москва · UTC+3", "Europe/Moscow"), ("Екатеринбург · UTC+5", "Asia/Yekaterinburg"), ("Новосибирск · UTC+7", "Asia/Novosibirsk"), ("Владивосток · UTC+10", "Asia/Vladivostok")]),
    ("hours", "Когда обычно играешь по своему времени? Укажи часы, например 19-23 или 22-2. Расписание ежедневное.", None),
    ("bio", "Расскажи о себе и стиле игры. До 500 символов. Отправь «-», чтобы пропустить.", None),
]


def profile_text(profile, *, developer_preview=False):
    def names(values):
        return ", ".join(value if len(value) <= 64 else value[:61] + "…" for value in values)
    selected = profile.get("selected_games", profile["games"])
    games_text = f"🎮 Актуальные игры: {escape(names(selected))}" if selected else ""
    steam_games = profile.get("steam_games", [])
    if steam_games:
        games_text += "\n🕹 Из Steam: " + escape(names(steam_games[:8]))
        remaining = profile.get("steam_game_count", len(steam_games)) - min(8, len(steam_games))
        if remaining > 0:
            games_text += f" · ещё {remaining}"
    labelled_test = profile.get("is_test") and not developer_preview
    notice = ("🧪 <b>ТЕСТОВАЯ АНКЕТА</b>\nИмя, возраст и интересы вымышлены. Фото — иллюстрация.\n\n"
              if labelled_test else "")
    source = (f'\n\n<a href="{escape(profile["photo_source_url"])}">Источник фото: Pinterest</a>'
              if labelled_test and profile.get("photo_source_url") else "")
    return notice + (
        f"<b>{escape(profile['name'])}, {profile['age']}</b>\n"
        f"{escape(profile['purpose_label'])}\n"
        f"{escape(profile['timezone'])} · {profile['start_hour']:02}:00–{profile['end_hour']:02}:00\n\n"
        f"{escape(profile['bio'])}\n\n"
        f"{games_text}"
    ) + source


def steam_status_text(profile):
    count = profile.get("steam_game_count", 0)
    status = profile.get("steam_import_status", "not_loaded")
    if status == "library":
        return f"Из библиотеки Steam сохранено игр: {count}." if count else "Steam вернул пустую библиотеку."
    if status == "recent":
        return f"Из Steam загружены недавние игры: {count}. Полная библиотека не получена."
    if status in {"profile_only", "error"}:
        text = "Последняя проверка не загрузила список игр Steam."
        if count:
            text += f" Сохранено игр из предыдущей загрузки: {count}."
        return text
    if count:
        return f"Из Steam сохранено игр: {count}."
    return "Игры из Steam ещё не загружены."


def build_bot(service, settings, steam, public_steam=None):
    bot = Bot(settings.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dispatcher = Dispatcher(storage=DatabaseStorage(service.db), events_isolation=SimpleEventIsolation())
    router = Router()
    router.message.filter(F.chat.type == "private")
    router.callback_query.filter(F.message.chat.type == "private")

    async def actor(message):
        user = service.user_by_telegram(message.from_user.id)
        if not user:
            await message.answer("Сначала заполни анкету: /start")
            return None
        if user.banned:
            await message.answer("Анкета заблокирована модератором. Удалить данные: /delete")
            return None
        return user

    async def prompt(message, state):
        data = await state.get_data()
        index = data["step_index"]
        if index >= len(STEPS):
            await state.update_data(photo_flow="registration")
            await state.set_state(Form.photo)
            await message.answer(
                f"<b>{len(STEPS) + 1}/{len(STEPS) + 2}</b> · Добавь фото анкеты.\n"
                "Отправь одну фотографию обычным фото через скрепку Telegram. "
                "Она будет видна людям в подборке. Можно продолжить без нового фото.",
                reply_markup=buttons([[("Продолжить без нового фото", "photo:keep")]])
            )
            return
        _, text, choices = STEPS[index]
        keyboard = buttons([[(label, f"form:{value}")] for label, value in choices]) if choices else None
        await state.set_state(Form.step)
        await message.answer(f"<b>{index + 1}/{len(STEPS) + 2}</b> · {text}", reply_markup=keyboard)

    async def consume_step(message, state, value):
        data = await state.get_data()
        index = data.get("step_index", 0)
        field, _, choices = STEPS[index]
        form = dict(data.get("form", {}))
        try:
            if choices and value not in {choice[1] for choice in choices}:
                raise ValueError("Выбери вариант кнопкой")
            if field == "age":
                age = int(value)
                if not 18 <= age <= 99:
                    raise ValueError("Возраст должен быть от 18 до 99")
                form[field] = age
            elif field == "name":
                if not 2 <= len(value.strip()) <= 40:
                    raise ValueError("Имя: от 2 до 40 символов")
                form[field] = value.strip()
            elif field in {"hours", "age_range"}:
                parts = re.fullmatch(r"\s*(\d{1,2})\s*[-–]\s*(\d{1,2})\s*", value)
                if not parts:
                    raise ValueError("Нужен диапазон через дефис, например 19-23")
                left, right = map(int, parts.groups())
                if field == "hours":
                    if not (0 <= left <= 23 and 0 <= right <= 24) or left == right or (left == 0 and right == 24):
                        raise ValueError("Часы от 0 до 24; интервал должен быть короче суток")
                    form.update(start_hour=left, end_hour=right)
                else:
                    if not 18 <= left <= right <= 99:
                        raise ValueError("Возрастной диапазон от 18 до 99")
                    form.update(age_min=left, age_max=right)
            elif field == "bio":
                if len(value) > 500:
                    raise ValueError("Описание должно быть короче 500 символов")
                form[field] = "" if value == "-" else value
            elif field == "timezone":
                from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
                try:
                    ZoneInfo(value)
                except (ZoneInfoNotFoundError, ValueError):
                    raise ValueError("Неизвестный часовой пояс. Пример: Asia/Novosibirsk")
                form[field] = value
            else:
                form[field] = value
        except ValueError as error:
            await message.answer(escape(str(error)))
            return
        await state.update_data(form=form, step_index=index + 1)
        await prompt(message, state)

    async def show_games(message, state, query=""):
        data = await state.get_data()
        selected = data.get("selected", [])
        catalog = service.db.catalog()
        games = [game for game in catalog if not query or any(query.casefold() in text.casefold() for text in [game.name, *game.aliases])][:12]
        rows = [[(f"{'✓ ' if game.app_id in selected else ''}{game.name}", f"game:{game.app_id}")] for game in games]
        rows.append([("Сохранить анкету", "game:save")])
        names = [game.name for game in catalog if game.app_id in selected]
        await message.answer(f"Выбрано {len(selected)}/10: {escape(', '.join(names)) or 'пока ничего'}\nПоиск: отправь название игры.", reply_markup=buttons(rows))

    async def send_card(message, profile, text=None, reply_markup=None, *, developer_preview=False):
        text = text or profile_text(profile, developer_preview=developer_preview)
        photo = service.photo_file(profile["id"])
        local_test_photo = service.test_photo_file(profile["id"]) if profile.get("is_test") and not photo else None
        if local_test_photo:
            photo = FSInputFile(local_test_photo)
        if photo:
            # Telegram photo captions have a smaller limit than regular messages.
            short = len(text.encode("utf-16-le")) // 2 <= 1024
            caption = text if short else ("🧪 ТЕСТОВАЯ АНКЕТА\n" if profile.get("is_test") and not developer_preview else "") + f"<b>{escape(profile['name'])}, {profile['age']}</b>"
            try:
                sent = await message.answer_photo(photo, caption=caption,
                                                  reply_markup=reply_markup if short else None)
                if local_test_photo and sent.photo:
                    service.cache_test_photo(profile["id"],max(sent.photo,key=lambda item:item.width*item.height).file_id)
                if short:
                    return
            except TelegramBadRequest:
                log.warning("Profile photo could not be sent; using text card")
        await message.answer(text, reply_markup=reply_markup)

    async def finish_photo(message, state, file_id=PHOTO_UNCHANGED):
        data = await state.get_data()
        if data.get("photo_flow") == "registration":
            if file_id is not PHOTO_UNCHANGED:
                await state.update_data(draft_photo_file_id=file_id)
            await state.set_state(Form.games)
            await message.answer("Выбери от 1 до 10 актуальных игр. Напиши название для поиска или выбирай кнопками. Steam можно подключить после сохранения.")
            await show_games(message, state)
        else:
            await state.clear()
            text = ("Вернулись в меню." if file_id is PHOTO_UNCHANGED else
                    "Фото удалено из анкеты." if file_id is None else "Фото сохранено.")
            await message.answer(text, reply_markup=menu())

    async def show_feed(message, user):
        rows = service.feed(user.id, limit=1)
        if not rows:
            text = "Подходящих новых анкет пока нет. Проверь фильтры или вернись позже."
            if not user.active:
                text = "Анкета на паузе. Включить показы: /pause"
            await message.answer(text, reply_markup=menu())
            return
        row = rows[0]
        common = ", ".join(row["common_games"])
        why = f"Общие игры: {common}" if common else "Близкие игровые интересы"
        text = (profile_text(row["profile"]) + f"\n\n💜 {escape(why)}\n"
                f"🕒 Пересечение расписания: ~{row['overlap_hours']} ч/день\n"
                f"Оценка сходства: {row['score']}/100 — это сходство интересов, а не вероятность отношений.")
        target = row["profile"]["id"]
        rows = [[("💜 Нравится", f"react:like:{target}"), ("Дальше →", f"react:skip:{target}")]]
        if not row["profile"]["is_test"]:
            rows.append([("Заблокировать", f"react:block:{target}"), ("Пожаловаться", f"react:report:{target}")])
        await send_card(message, row["profile"], text, reply_markup=buttons(rows))

    async def show_matches(message, user):
        rows = service.matches(user.id)
        if not rows:
            await message.answer("Здесь появятся взаимные симпатии 💜", reply_markup=menu())
        for row in rows[:30]:
            await send_card(message, row["profile"], reply_markup=buttons([
                [("💬 Написать через бота", f"chat:{row['id']}")],
                [("Поделиться моим @username", f"contact:{row['id']}")],
                [("Больше не общаться", f"react:block:{row['profile']['id']}")],
                [("Мы пообщались / сыграли", f"feedback:{row['id']}")],
            ]))

    async def show_test_feed(message,user):
        rows = service.feed(user.id,limit=1,test_only=True)
        if not rows:
            await message.answer("Тестовые анкеты просмотрены.",reply_markup=buttons([
                [("Посмотреть сначала", "test:restart")]
            ]))
            return
        row = rows[0]
        text = profile_text(row["profile"]) + f"\n\nСходство игровых интересов: {row['score']}/100."
        target = row["profile"]["id"]
        await send_card(message,row["profile"],text,reply_markup=buttons([
            [("💜 Тестовый лайк",f"testreact:like:{target}"),("Дальше →",f"testreact:skip:{target}")],
            [("Начать сначала","test:restart")],
        ]))

    @router.message(Command("privacy"))
    async def privacy(message: Message):
        await message.answer(
            "<b>Данные и видимость</b>\n"
            "Храним анкету, Telegram ID, username, ссылку Telegram на фото анкеты, предпочтения, выбор игр, реакции, жалобы и события показов. "
            "После подключения Steam — SteamID, доступную библиотеку и игровое время. "
            "Они используются для подбора людей; пароль Steam не запрашиваем.\n\n"
            "В ленте показываем загруженное фото, имя, возраст, описание, цель, часовой пояс, расписание, выбранные и импортированные из Steam игры. "
            "Telegram ID и SteamID публично не показываем. Контакт передаётся только по кнопке «Поделиться». "
            "Назначенные операторы и разработчики могут просматривать и исправлять анкеты через закрытую панель; изменения журналируются. "
            "Сообщения через бота обрабатываются сервером и доставляются адресату; текст в БД не сохраняется. "
            "Это не секретный чат Telegram.\n\n"
            f"Хранение: {escape(settings.data_location)}. Контакт: {escape(settings.operator_contact)}.\n"
            "Анкеты без активности 30 дней скрываются. /pause — пауза, /steam — отключение Steam, "
            "/photo — заменить или убрать фото, /delete — удаление анкеты и связанных записей из рабочей БД. "
            "Сами фото хранятся в Telegram; при удалении из анкеты убирается наша ссылка на фото. "
            "Доставленные сообщения и фото у адресатов остаются. "
            "Если оператор делает резервные копии, срок их удаления должен быть указан им отдельно."
        )

    @router.message(Command("about"))
    async def about(message):
        model_link = (f'<a href="{escape(settings.public_url)}/model">Модель, лицензии и метрики</a>'
                      if settings.public_url.startswith("https://") else "/model — скачать модель и отчёт")
        await message.answer(
            "PartyUp · одна анкета, единые настройки, общие игры. 18+.\n"
            "Рекомендации: игры 65%, жанры 25%, расписание 10%. Веса экспериментальные.\n"
            "Используется модель игровых интересов на Steam Video Games (Tamber, Kaggle, 2017), "
            "ODbL 1.0 / DbCL 1.0. Это не модель романтической совместимости.\n"
            '<a href="https://www.kaggle.com/datasets/tamber/steam-video-games">Источник данных</a> · '
            + model_link
        )

    @router.message(Command("model"))
    async def download_model(message):
        await message.answer_document(FSInputFile(ROOT / "data/affinity.json"), caption=(
            "Производная база связей игр · Steam Video Games / Tamber, Kaggle. "
            "ODbL 1.0 / DbCL 1.0. Источник: https://www.kaggle.com/datasets/tamber/steam-video-games\n"
            "Лицензии: https://opendatacommons.org/licenses/odbl/1-0/ и "
            "https://opendatacommons.org/licenses/dbcl/1-0/"
        ))
        await message.answer_document(FSInputFile(ROOT / "data/model_report.json"),
                                      caption="Метрики и ограничения модели игровых интересов")

    @router.message(Command("cancel"))
    async def cancel(message, state: FSMContext):
        await state.clear()
        await message.answer("Вернулись в меню.", reply_markup=menu())

    @router.message(Command("delete"))
    async def delete_prompt(message, state: FSMContext):
        await state.clear()
        await message.answer("Удалить анкету, Steam, реакции, совпадения и черновик?", reply_markup=buttons([
            [("Удалить мои данные", "delete:yes"), ("Отмена", "delete:no")]
        ]))

    @router.callback_query(F.data.startswith("delete:"))
    async def delete_confirm(callback, state: FSMContext):
        await callback.answer()
        if callback.data == "delete:yes":
            await state.clear()
            service.delete_user(callback.from_user.id)
            await callback.message.answer("Данные удалены. /start — создать новую анкету.")
        else:
            await callback.message.answer("Удаление отменено.", reply_markup=menu())
        await callback.message.edit_reply_markup(reply_markup=None)

    @router.message(CommandStart())
    async def start(message, state: FSMContext):
        user = service.user_by_telegram(message.from_user.id)
        if user:
            await state.clear()
            await message.answer("Твои люди где-то рядом. Посмотрим, во что играют?", reply_markup=menu())
            return
        await state.set_state(Form.consent)
        await message.answer("<b>PartyUp · найди своих</b>\nЗнакомства, отношения и пати через общие игры. "
                             "Одна анкета, никаких отдельных режимов.\n\nБот 18+. Перед регистрацией прочитай /privacy. "
                             "Подтверди совершеннолетие и согласие на обработку данных для работы бота.",
                             reply_markup=buttons([[("Мне 18+, согласен / согласна", "consent:yes")]]))

    @router.callback_query(Form.consent, F.data == "consent:yes")
    async def consent(callback, state: FSMContext):
        await callback.answer()
        await state.set_data({"step_index": 0, "form": {}, "selected": []})
        await prompt(callback.message, state)

    @router.message(Command("edit"))
    async def edit(message, state: FSMContext):
        user = await actor(message)
        if user:
            await state.set_data({"step_index": 0, "form": {}, "selected": [entry.app_id for entry in user.games if entry.selected]})
            await prompt(message, state)

    @router.message(Command("photo"))
    @router.message(F.text == "📷 Фото анкеты")
    async def edit_photo(message, state: FSMContext):
        user = await actor(message)
        if not user:
            return
        await state.set_data({"photo_flow": "profile"})
        await state.set_state(Form.photo)
        rows = [[("Оставить как есть", "photo:keep")]]
        if user.photo_file_id:
            rows.insert(0, [("Удалить фото из анкеты", "photo:remove")])
        await message.answer("Пришли одну фотографию обычным фото через скрепку Telegram. "
                             "Она заменит текущее фото и будет видна в карточке анкеты. "
                             "/cancel — отмена.", reply_markup=buttons(rows))

    @router.message(Form.photo, F.photo)
    async def receive_photo(message, state: FSMContext):
        photo = max(message.photo, key=lambda item: item.width * item.height)
        if photo.file_size is not None and photo.file_size > 10 * 1024 * 1024:
            await message.answer("Фото слишком большое. Отправь его как обычную фотографию до 10 МБ.")
            return
        data = await state.get_data()
        try:
            if data.get("photo_flow") == "registration":
                service.validate_photo(photo.file_id)
            else:
                user = await actor(message)
                if not user:
                    return
                service.set_photo(user.id, photo.file_id)
            await finish_photo(message, state, photo.file_id)
        except DomainError as error:
            await message.answer(escape(str(error)))

    @router.callback_query(Form.photo, F.data.in_({"photo:keep", "photo:remove"}))
    async def photo_choice(callback, state: FSMContext):
        await callback.answer()
        data = await state.get_data()
        removed = callback.data == "photo:remove"
        if removed and data.get("photo_flow") != "registration":
            user = service.user_by_telegram(callback.from_user.id)
            if not user or user.banned:
                await callback.message.answer("Анкета недоступна.")
                return
            service.set_photo(user.id, None)
        await callback.message.edit_reply_markup(reply_markup=None)
        await finish_photo(callback.message, state, None if removed else PHOTO_UNCHANGED)

    @router.message(Command("games"))
    async def edit_games(message, state: FSMContext):
        user = await actor(message)
        if user:
            profile = ProfileInput.model_validate(service.public_profile(user))
            await state.set_data({"form": profile.model_dump(exclude={"game_ids"}),
                                  "selected": profile.game_ids})
            await state.set_state(Form.games)
            await show_games(message, state)

    @router.message(Command("pause"))
    async def pause(message):
        user = await actor(message)
        if user:
            active = service.toggle_active(user.id)
            await message.answer("Анкета снова в поиске." if active else "Анкета на паузе. Возобновить: /pause")

    @router.message(Command("profile"))
    @router.message(F.text == "👤 Моя анкета")
    async def own_profile(message):
        user = await actor(message)
        if user:
            profile = service.public_profile(user)
            await send_card(message, profile, profile_text(profile) + "\n\n/edit — изменить\n/photo — добавить, заменить или убрать фото\n/pause — пауза\n/delete — удалить")

    @router.message(Command("find"))
    @router.message(F.text == "🔎 Найти своих")
    async def find(message, state: FSMContext):
        await state.clear()
        user = await actor(message)
        if user:
            await show_feed(message, user)

    @router.message(Command("matches"))
    @router.message(F.text == "💜 Совпадения")
    async def matches(message, state: FSMContext):
        await state.clear()
        user = await actor(message)
        if user:
            await show_matches(message, user)

    @router.message(Command("test"))
    async def test_profiles(message,state:FSMContext):
        user = await actor(message)
        if not user:
            return
        await state.clear()
        await message.answer(f"🧪 Тестовых анкет с фотографиями Pinterest: {len(fixture_definitions())}. "
                             "Это примеры для проверки карточек и кнопок. В тестовом просмотре "
                             "возрастные фильтры и предпочтения не применяются. /find — обычный поиск.",
                             reply_markup=menu())
        await show_test_feed(message,user)

    @router.callback_query(F.data == "test:restart")
    async def restart_test_profiles(callback,state:FSMContext):
        await callback.answer()
        user = service.user_by_telegram(callback.from_user.id)
        if not user or user.banned:
            return
        await state.clear()
        service.reset_test_reactions(user.id)
        await callback.message.edit_reply_markup(reply_markup=None)
        await show_test_feed(callback.message,user)

    @router.message(Command("steam"))
    @router.message(F.text == "🎮 Steam")
    async def steam_menu(message, state: FSMContext):
        user = await actor(message)
        if not user:
            return
        rows = [
            [InlineKeyboardButton(text="Добавить / изменить ссылку", callback_data="steam:add")],
            [InlineKeyboardButton(text="Обновить библиотеку", callback_data="steam:sync")],
            [InlineKeyboardButton(text="Отключить Steam и удалить импорт", callback_data="steam:disconnect")],
        ]
        if settings.steam_mode == "openid":
            ticket = service.login_ticket(user.id)
            rows.insert(0, [InlineKeyboardButton(text="Подтвердить владение через Steam", url=f"{settings.public_url}/steam/login?ticket={ticket}")])
        await state.set_state(Form.steam_link)
        status = ("Ссылка Steam сохранена. " + ("Владение подтверждено." if user.steam_verified else "Владение аккаунтом не подтверждено.")) if user.steam_id else "Добавим игровой профиль по ссылке."
        status += "\n" + steam_status_text(service.public_profile(user))
        await message.answer(status + "\n\nПришли ссылку вида https://steamcommunity.com/id/ник или "
                             "https://steamcommunity.com/profiles/SteamID. Получим доступные открытые игры. "
                             "Если Steam требует входа или скрывает библиотеку, выбрать игры можно через /games. "
                             "Пароль и API-ключ не нужны. /cancel — вернуться.",
                             reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))

    async def import_public(message, user, value):
        if public_steam is None:
            raise DomainError("Парсер Steam временно недоступен")
        profile = await public_steam.fetch(value)
        service.link_public_profile(user.id, profile.steam_id)
        if profile.coverage != "profile_only":
            service.import_games(user.id, profile.games, expected_steam_id=profile.steam_id,
                                 complete=profile.coverage == "library")
        else:
            service.mark_steam_checked(user.id)
        text = "Ссылка Steam сохранена.\n" + steam_status_text(service.public_profile(service.user(user.id)))
        if profile.games:
            preview = ", ".join(str(game.get("name", "Steam"))[:64] for game in profile.games[:5])
            text += "\nВ анкете теперь видны: " + preview
        if profile.warning:
            text += "\n" + profile.warning
        text += "\n/profile — посмотреть анкету. /games — выбрать актуальные игры."
        await message.answer(escape(text), reply_markup=menu())

    @router.callback_query(F.data.startswith("steam:"))
    async def steam_action(callback, state: FSMContext):
        await callback.answer()
        user = service.user_by_telegram(callback.from_user.id)
        if not user or user.banned:
            return
        if callback.data == "steam:add":
            await state.set_state(Form.steam_link)
            await callback.message.answer("Пришли ссылку на свой профиль steamcommunity.com. /cancel — вернуться.")
            return
        if callback.data == "steam:disconnect":
            service.disconnect_steam(user.id)
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
                await callback.message.answer(steam_status_text(service.public_profile(user))
                                              + f"\nСледующая проверка доступна через {wait_minutes} мин.")
                return
        try:
            if settings.steam_api_key:
                games = await steam.owned_games(user.steam_id)
                count = service.import_games(user.id, games, expected_steam_id=user.steam_id)
                await callback.message.answer(f"Импортировано игр: {count}. Выбранные тобой интересы сохранены.")
            else:
                await import_public(callback.message, user, "https://steamcommunity.com/profiles/" + user.steam_id)
            await state.clear()
        except DomainError as error:
            await callback.message.answer(escape(str(error)))

    @router.message(Form.steam_link, F.text)
    async def steam_link_message(message, state: FSMContext):
        user = await actor(message)
        if not user:
            return
        data = await state.get_data()
        if data.get("steam_attempt_at") and (utcnow() - datetime.fromisoformat(data["steam_attempt_at"])).total_seconds() < 10:
            await message.answer("Подожди несколько секунд перед следующим запросом Steam.")
            return
        await state.update_data(steam_attempt_at=utcnow().isoformat())
        try:
            await import_public(message, user, message.text)
            await state.clear()
        except DomainError as error:
            await message.answer(escape(str(error)))

    @router.callback_query(F.data.startswith("react:") | F.data.startswith("testreact:"))
    async def react(callback):
        await callback.answer()
        user = service.user_by_telegram(callback.from_user.id)
        if not user:
            return
        origin, kind, target = callback.data.split(":")
        try:
            result = service.react(user.id, int(target), kind)
        except DomainError as error:
            await callback.message.answer(escape(str(error)))
            return
        await callback.message.edit_reply_markup(reply_markup=None)
        if result.get("test_profile"):
            if kind == "like" and origin == "testreact":
                await callback.message.answer("Тестовый лайк сохранён ✓")
            if origin == "testreact":
                await show_test_feed(callback.message,service.user(user.id))
            else:
                await show_feed(callback.message,service.user(user.id))
            return
        if kind == "report":
            await callback.message.answer("Жалоба отправлена в очередь модерации. Анкета также заблокирована для тебя.")
        if result["matched"] and result["new"]:
            peer = service.user(int(target))
            markup = buttons([[("Начать общение", f"chat:{result['match_id']}")]])
            await callback.message.answer(f"💜 Взаимно! Вы понравились друг другу с {escape(peer.name)}.", reply_markup=markup)
            try:
                await bot.send_message(peer.telegram_id, f"💜 Взаимно! Ты понравился / понравилась {escape(user.name)}.", reply_markup=markup)
            except TelegramForbiddenError:
                pass
        await show_feed(callback.message, service.user(user.id))

    @router.callback_query(F.data.startswith("chat:"))
    async def begin_chat(callback, state: FSMContext):
        await callback.answer()
        user = service.user_by_telegram(callback.from_user.id)
        if not user:
            return
        try:
            match_id = int(callback.data.split(":")[1])
            peer = service.match_peer(user.id, match_id)
        except DomainError as error:
            await callback.message.answer(escape(str(error)))
            return
        await state.set_state(Form.chat)
        await state.set_data({"match_id": match_id})
        await callback.message.answer(f"Пишешь {escape(peer.name)} через бота. Отправляй текст. /cancel — выйти. "
                                      "Твой Telegram ID не передаётся; текст виден получателю и обрабатывается сервером.")

    @router.callback_query(F.data.startswith("contact:"))
    async def share_contact(callback):
        await callback.answer()
        user = service.user_by_telegram(callback.from_user.id)
        if not user:
            return
        try:
            peer = service.match_peer(user.id, int(callback.data.split(":")[1]))
        except DomainError as error:
            await callback.message.answer(escape(str(error)))
            return
        if not callback.from_user.username:
            await callback.message.answer("У тебя нет @username. Можно общаться через бота или добавить username в Telegram.")
            return
        try:
            await bot.send_message(peer.telegram_id, f"{escape(user.name)} поделился / поделилась контактом: @{escape(callback.from_user.username)}")
            await callback.message.answer("Твой контакт отправлен этому человеку.")
        except TelegramForbiddenError:
            await callback.message.answer("Сообщение не доставлено: пользователь отключил бота.")

    @router.callback_query(F.data.startswith("feedback:"))
    async def feedback(callback):
        await callback.answer()
        user = service.user_by_telegram(callback.from_user.id)
        if user:
            try:
                peer = service.match_peer(user.id, int(callback.data.split(":")[1]))
                service.record(user.id, peer.id, "positive_feedback")
                await callback.message.answer("Спасибо! Учтём, что знакомство состоялось.")
            except DomainError:
                await callback.message.answer("Совпадение недоступно.")

    @router.message(Command("reports"))
    async def reports(message):
        if message.from_user.id not in settings.admins:
            return
        with service.db.session() as session:
            rows = list(session.scalars(select(Report).where(Report.resolved.is_(False)).limit(20)))
            await message.answer("Нет новых жалоб." if not rows else "\n".join(
                f"Жалоба #{row.id}: анкета {row.target_id} · {escape(row.reason)}" for row in rows
            ) + "\n/ban ID — заблокировать, /unban ID — разблокировать")

    @router.message(Command("ban", "unban"))
    async def moderate(message):
        if message.from_user.id not in settings.admins:
            return
        parts = message.text.split()
        if len(parts) != 2 or not parts[1].isdigit():
            await message.answer("Формат: /ban ID или /unban ID")
            return
        with service.db.session.begin() as session:
            user = session.get(User, int(parts[1]))
            if user:
                user.banned = parts[0].split("@")[0] == "/ban"
                if user.banned:
                    for report in session.scalars(select(Report).where(Report.target_id == user.id)):
                        report.resolved = True
        await message.answer("Статус обновлён." if user else "Анкета не найдена.")

    @router.message(Form.step, F.text)
    async def step_text(message, state: FSMContext):
        await consume_step(message, state, message.text)

    @router.callback_query(Form.step, F.data.startswith("form:"))
    async def step_button(callback, state: FSMContext):
        await callback.answer()
        await consume_step(callback.message, state, callback.data.split(":", 1)[1])
        await callback.message.edit_reply_markup(reply_markup=None)

    @router.message(Form.games, F.text)
    async def game_search(message, state: FSMContext):
        await show_games(message, state, message.text.strip())

    @router.callback_query(Form.games, F.data.startswith("game:"))
    async def game_button(callback, state: FSMContext):
        await callback.answer()
        data = await state.get_data()
        selected = list(data.get("selected", []))
        value = callback.data.split(":")[1]
        if value == "save":
            try:
                profile = ProfileInput(**data["form"], game_ids=selected)
                service.save_profile(callback.from_user.id, profile, callback.from_user.username,
                                     photo_file_id=data.get("draft_photo_file_id", PHOTO_UNCHANGED))
            except (ValidationError, DomainError) as error:
                await callback.message.answer("Не удалось сохранить. Выбери 1–10 игр и проверь анкету. " + escape(str(error)[:300]))
                return
            await state.clear()
            await callback.message.edit_reply_markup(reply_markup=None)
            await callback.message.answer("Анкета готова 💜 Пришли ссылку Steam через /steam или ищи своих. "
                                          "/photo — добавить или заменить фото.", reply_markup=menu())
            return
        app_id = int(value)
        if app_id in selected:
            selected.remove(app_id)
        elif len(selected) < 10:
            selected.append(app_id)
        await state.update_data(selected=selected)
        await callback.message.edit_reply_markup(reply_markup=None)
        await show_games(callback.message, state)

    @router.message(Form.chat, F.text)
    async def relay(message, state: FSMContext):
        user = await actor(message)
        if not user:
            return
        data = await state.get_data()
        try:
            peer = service.match_peer(user.id, data["match_id"])
            with service.db.session() as session:
                recent = list(session.scalars(select(Event.id).where(
                    Event.actor_id == user.id, Event.kind == "message_sent",
                    Event.created_at > utcnow() - timedelta(minutes=1)
                )))
            if len(recent) >= 15:
                raise DomainError("Слишком много сообщений. Подожди минуту")
            if len(message.text) > 3000:
                raise DomainError("Сообщение должно быть короче 3000 символов")
            await bot.send_message(peer.telegram_id, f"💬 <b>{escape(user.name)}</b>\n{escape(message.text)}",
                                   reply_markup=buttons([[("Ответить", f"chat:{data['match_id']}")],
                                                         [("Заблокировать", f"react:block:{user.id}")],
                                                         [("Пожаловаться", f"react:report:{user.id}")]]))
            service.record(user.id, peer.id, "message_sent")
            await message.answer("Доставлено ✓")
        except DomainError as error:
            await message.answer(escape(str(error)))
        except TelegramForbiddenError:
            await message.answer("Не доставлено: собеседник отключил бота.")

    @router.message(Form.photo)
    async def unsupported_photo(message):
        await message.answer("Нужно обычное фото: скрепка → Фото или видео. "
                             "Изображения файлом, видео и ссылки пока не подходят. "
                             "Можно продолжить без нового фото или выйти через /cancel.")

    @router.message(F.photo)
    async def photo_hint(message, state: FSMContext):
        if await state.get_state() == Form.chat.state:
            await message.answer("В переписке через бота пока поддерживается текст. Фото анкеты меняется через /photo.")
        else:
            await message.answer("Для фото анкеты нажми /photo или «📷 Фото анкеты». "
                                 "При регистрации фото добавляется после описания.")

    @router.message()
    async def fallback(message):
        await message.answer("Используй меню или /start. /cancel — выйти из заполнения анкеты или чата.", reply_markup=menu())

    @router.callback_query()
    async def stale(callback):
        await callback.answer("Эта кнопка устарела. Открой меню заново.")

    @router.errors()
    async def on_error(event):
        # Do not log Update objects: they may include private message contents.
        log.error("Bot handler failed: %s", type(event.exception).__name__)
        if event.update.message:
            try:
                await event.update.message.answer("Не получилось выполнить действие. Повтори или вернись в меню: /cancel")
            except TelegramAPIError:
                pass
        return True

    from .developer_bot import build_developer_router
    dispatcher.include_router(build_developer_router(service,settings,send_card,profile_text,menu))
    dispatcher.include_router(router)
    return bot, dispatcher


async def run_polling(bot, dispatcher, settings=None):
    commands=[
        BotCommand(command="find", description="Найти своих"),
        BotCommand(command="profile", description="Моя анкета"),
        BotCommand(command="photo", description="Добавить или изменить фото анкеты"),
        BotCommand(command="matches", description="Взаимные симпатии"),
        BotCommand(command="steam", description="Подключить Steam"),
        BotCommand(command="games", description="Выбрать актуальные игры"),
        BotCommand(command="test", description="Тестовые анкеты с фотографиями"),
        BotCommand(command="edit", description="Изменить общие настройки"),
        BotCommand(command="pause", description="Пауза или возобновление"),
        BotCommand(command="privacy", description="Данные и приватность"),
        BotCommand(command="delete", description="Удалить мои данные"),
        BotCommand(command="cancel", description="Вернуться в меню"),
        BotCommand(command="id", description="Мой Telegram ID"),
    ]
    await bot.set_my_commands(commands)
    if settings:
        for developer_id in settings.developers:
            try:
                await bot.set_my_commands([*commands,BotCommand(command="dev",description="Панель разработчика")],
                                          scope=BotCommandScopeChat(chat_id=developer_id))
            except TelegramAPIError:
                log.warning("Could not set private developer command menu")
    await dispatcher.start_polling(bot, handle_signals=False, close_bot_session=False,
                                   allowed_updates=dispatcher.resolve_used_update_types())
