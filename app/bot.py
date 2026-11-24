import asyncio
import logging
import math
import re
from dataclasses import asdict
from datetime import datetime, timedelta
from html import escape
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher, F, Router
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import SimpleEventIsolation
from aiogram.types import BotCommand, BotCommandScopeChat, FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto, KeyboardButton, Message, ReplyKeyboardMarkup, WebAppInfo
from pydantic import ValidationError
from sqlalchemy import select

from .models import Game, Report, User, utcnow
from .config import ROOT
from .dota import DOTA_APP_ID, dota_game_name
from .recommender import CARD_GAME_LIMIT, steam_card_game_ids
from .schemas import ProfileInput
from .service import DomainError, PHOTO_UNCHANGED, PURPOSES
from .steam_public import PublicProfile
from .storage import DatabaseStorage
from .test_profiles import fixture_definitions

log = logging.getLogger(__name__)

CONTACT_NOTICE = "При взаимном лайке бот покажет собеседнику твой Telegram @username."
DEFAULT_AVATAR = ROOT / "app/assets/default-avatar.png"


class Form(StatesGroup):
    consent = State()
    step = State()
    games = State()
    game_source = State()
    registration_steam = State()
    steam_link = State()
    photo = State()
    review = State()
    focus = State()


def buttons(rows):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=text, callback_data=data) for text, data in row] for row in rows
    ])


def menu():
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="🔎 Найти своих"), KeyboardButton(text="💜 Совпадения")],
        [KeyboardButton(text="👤 Моя анкета"), KeyboardButton(text="🎮 Steam")],
        [KeyboardButton(text="🎮 Хочу сыграть сейчас")],
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
    ("hours", "Когда обычно играешь по своему времени? Выбери или напиши свой интервал, например 21-23.",
     [("Днём · 12–17", "12-17"), ("Вечером · 19–23", "19-23"), ("Ночью · 22–02", "22-2")]),
    ("bio", "Расскажи о себе и стиле игры. До 500 символов.",
     [("Без описания", "-")]),
]
FIELD_STEPS = {field: (prompt, choices) for field, prompt, choices in STEPS}
REGISTRATION_FIELDS = ("name", "age", "timezone", "bio")
PREVIOUS_REGISTRATION_FIELDS = ("name", "age", "purpose", "timezone", "hours")
EDIT_FIELDS = (
    (("Имя", "name"), ("Возраст", "age")),
    (("Цель знакомства", "purpose"), ("Описание", "bio")),
    (("Мой пол", "gender"), ("Кого показывать", "looking_for")),
    (("Возраст собеседника", "age_range"), ("Язык общения", "language")),
    (("Часовой пояс", "timezone"), ("Время игры", "hours")),
)


def registration_fields(data):
    if data.get("new_registration"):
        return REGISTRATION_FIELDS
    if data.get("short_registration"):
        return PREVIOUS_REGISTRATION_FIELDS
    return tuple(item[0] for item in STEPS)


def avatar_buttons(*, back=None, allow_removal=False):
    rows = [[("Загрузить фото", "photo:upload")], [("Выбрать стандартное", "photo:default")]]
    if allow_removal:
        rows.append([("Удалить фото из анкеты", "photo:remove")])
    if back:
        rows.append([("← Назад", back)])
    return buttons(rows)


def profile_heading(profile):
    offset = datetime.now(ZoneInfo(profile["timezone"])).strftime("%z")
    minutes = f":{offset[3:]}" if offset[3:] != "00" else ""
    return f"<b>{escape(profile['name'])}</b> (UTC{offset[0]}{int(offset[1:3])}{minutes})"


def card_game_name(name):
    return escape(name[:47] + "…" if len(name) > 48 else name)


def profile_text(profile, *, common_games=()):
    sections = [profile_heading(profile)]
    if profile["games"]:
        names = [card_game_name(name) for name in profile["games"][:CARD_GAME_LIMIT]]
        sections.append("<b>🎮 Игры</b>\n" + "\n".join(f"• {name}" for name in names))
    if common_games:
        sections.append("<b>🤝 Общие игры</b>\n" + " · ".join(map(card_game_name, common_games)))
    bio = profile["bio"]
    if bio:
        sections.append("<b>О себе</b>\n" + escape(bio))
    return "\n\n".join(sections)


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
        fields = registration_fields(data)
        if index >= len(fields) and data.get("new_registration"):
            await prompt_registration_avatar(message, state)
            return
        if index >= len(fields) and data.get("short_registration"):
            await show_game_source(message, state)
            return
        if index >= len(fields):
            await state.update_data(photo_flow="registration")
            await state.set_state(Form.photo)
            await message.answer(
                f"<b>{len(STEPS) + 1}/{len(STEPS) + 2}</b> · Добавь фото анкеты.\n"
                "Отправь одну фотографию обычным фото через скрепку Telegram. "
                "Она будет видна людям в подборке. Можно продолжить без нового фото.",
                reply_markup=buttons([[("Продолжить без нового фото", "photo:keep")]])
            )
            return
        await show_field(message, state, fields[index], prefix=f"<b>{index + 1}/{len(fields) + 1}</b> · ",
                         back="form:back" if index > 0 else None)

    async def prompt_registration_avatar(message, state):
        await state.update_data(photo_flow="registration")
        await state.set_state(Form.photo)
        await message.answer("Выбери аватар для анкеты.", reply_markup=avatar_buttons(back="form:back"))

    async def show_field(message, state, field, *, prefix="", back=None):
        prompt_text, choices = FIELD_STEPS[field]
        rows = [[(label, f"form:{field}:{value}")] for label, value in choices or []]
        if back:
            rows.append([("← Назад", back)])
        await state.set_state(Form.step)
        await message.answer(prefix + prompt_text, reply_markup=buttons(rows) if rows else None)

    async def consume_step(message, state, sender, value, expected_field=None):
        data = await state.get_data()
        index = data.get("step_index", 0)
        fields = registration_fields(data)
        field = data.get("edit_field") or fields[index]
        if expected_field is not None and expected_field != field:
            await message.answer("Эта кнопка относится к предыдущему шагу.")
            return
        _, choices = FIELD_STEPS[field]
        form = {}
        try:
            if choices and field not in {"hours", "bio", "timezone"} and value not in {choice[1] for choice in choices}:
                raise ValueError("Выбери вариант кнопкой")
            if field == "age":
                age = int(value)
                if not 18 <= age <= 99:
                    raise ValueError("Возраст должен быть от 18 до 99")
                form[field] = age
            elif field == "name":
                if not 2 <= len(value.strip()) <= 40:
                    raise ValueError("Имя: от 2 до 40 символов")
                form[field] = ProfileInput.clean_text(value)
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
                form[field] = "" if value == "-" else ProfileInput.clean_text(value)
            elif field == "timezone":
                from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
                try:
                    ZoneInfo(value)
                except (ZoneInfoNotFoundError, ValueError):
                    raise ValueError("Неизвестный часовой пояс. Пример: Asia/Novosibirsk")
                form[field] = value
            else:
                form[field] = value
            if data.get("edit_field") and not data.get("edit_draft"):
                service.save_profile(sender.id, None, sender.username, changes=form)
        except (ValueError, DomainError) as error:
            await message.answer(escape(str(error)))
            return
        if data.get("edit_field") and not data.get("edit_draft"):
            await state.clear()
            await show_edit_menu(message, prefix="Сохранено ✓\n")
            return
        form = {**data.get("form", {}), **form}
        if data.get("edit_draft"):
            await state.update_data(form=form, edit_field=None, edit_draft=False)
            await review_profile(message, state)
            return
        await state.update_data(form=form, step_index=index + 1)
        await prompt(message, state)

    def registration_catalog(data):
        catalog = {game.app_id: game for game in service.db.catalog()}
        for row in (data.get("draft_steam") or {}).get("games", []):
            app_id = row["appid"]
            if app_id not in catalog:
                catalog[app_id] = Game(app_id=app_id, name=row.get("name", f"Steam {app_id}"), aliases=[])
        return catalog

    async def show_game_source(message, state):
        await state.set_state(Form.game_source)
        await message.answer(
            "Подключи Steam, чтобы бот сам выбрал игры для анкеты и учёл всю доступную библиотеку в подборе.",
            reply_markup=buttons([
                [("🎮 Подключить Steam", "registration:steam")],
                [("Выбрать игры из списка", "registration:manual")],
                [("← Назад", "form:back")],
            ]),
        )

    async def show_games(message, state, query=""):
        data = await state.get_data()
        selected = data.get("selected", [])
        catalog = registration_catalog(data)
        query = query.casefold()
        games = sorted(catalog.values(), key=lambda game: (game.app_id not in selected, game.name.casefold()))
        games = [game for game in games if any(query in text.casefold() for text in [game.name, *game.aliases])][:12]
        registration = data.get("games_flow") != "edit"
        rows = [[(f"{'✓ ' if game.app_id in selected else ''}{game.name[:60]}", f"game:{game.app_id}")] for game in games]
        rows.append([("Посмотреть анкету" if registration else "Сохранить игры", "game:save")])
        if data.get("short_registration"):
            rows.append([("← Назад", "review:open" if data.get("reviewed") else "registration:source")])
        elif not registration:
            rows.append([("← К настройкам", "edit:menu")])
        names = [catalog[app_id].name[:55] for app_id in selected if app_id in catalog]
        await message.answer(f"Выбрано {len(selected)}/10: {escape(', '.join(names)) or 'пока ничего'}\n"
                             "Выбери актуальные игры кнопками или отправь название для поиска.",
                             reply_markup=buttons(rows))

    async def prompt_registration_steam(message):
        await message.answer(
            "Пришли ссылку на свой профиль steamcommunity.com. Получим доступные игры и часы для подбора, "
            "а для Dota 2 проверим публичную статистику в OpenDota.",
            reply_markup=buttons([[("← К выбору способа", "registration:source")]])
        )

    def card_photo(profile):
        photo = service.photo_file(profile["id"])
        if not photo and profile.get("use_default_avatar"):
            return FSInputFile(DEFAULT_AVATAR)
        local = service.test_photo_file(profile["id"]) if profile.get("is_test") and not photo else None
        return FSInputFile(local) if local else photo

    async def send_card(message, profile, text=None, reply_markup=None, *, draft_photo=PHOTO_UNCHANGED):
        text = text or profile_text(profile)
        photo = card_photo(profile) if draft_photo is PHOTO_UNCHANGED else draft_photo
        if photo:
            # Telegram photo captions have a smaller limit than regular messages.
            short = len(text.encode("utf-16-le")) // 2 <= 1024
            caption = text if short else profile_heading(profile)
            try:
                sent = await message.answer_photo(photo, caption=caption,
                                                  reply_markup=reply_markup if short else None)
                if isinstance(photo, FSInputFile) and profile.get("is_test") and sent.photo:
                    service.cache_test_photo(profile["id"],max(sent.photo,key=lambda item:item.width*item.height).file_id)
                if short:
                    return
            except TelegramBadRequest:
                log.warning("Profile photo could not be sent; using text card")
        await message.answer(text, reply_markup=reply_markup)

    async def finish_photo(message, state, file_id=PHOTO_UNCHANGED, *, use_default_avatar=False):
        data = await state.get_data()
        if data.get("photo_flow") in {"registration", "review"}:
            if file_id is not PHOTO_UNCHANGED:
                await state.update_data(draft_photo_file_id=file_id, draft_default_avatar=use_default_avatar)
            if data.get("photo_flow") == "review":
                await review_profile(message, state)
                return
            if data.get("new_registration"):
                await show_game_source(message, state)
                return
            await state.set_state(Form.games)
            await message.answer("Выбери от 1 до 10 актуальных игр. Напиши название для поиска или выбирай кнопками. Steam можно подключить после сохранения.")
            await show_games(message, state)
        else:
            await state.clear()
            text = ("Стандартный аватар установлен." if use_default_avatar else
                    "Вернулись в меню." if file_id is PHOTO_UNCHANGED else
                    "Фото удалено из анкеты." if file_id is None else "Фото сохранено.")
            await message.answer(text, reply_markup=menu())

    async def show_edit_menu(message, *, prefix="", draft=False):
        space = "review" if draft else "edit"
        rows = [[(label, f"{space}:{field}") for label, field in row] for row in EDIT_FIELDS]
        rows.extend([[("Игры", f"{space}:games"), ("Фото", f"{space}:photo")]])
        if draft:
            rows.append([("← К предпросмотру", "review:open")])
        else:
            rows.append([("← Моя анкета", "edit:profile")])
        await message.answer(prefix + "Что хочешь изменить?", reply_markup=buttons(rows))

    def draft_steam_highlights(data):
        steam = data.get("draft_steam") or {}
        played = {row["appid"]: (row["appid"], row.get("playtime_forever", 0), row.get("playtime_2weeks", 0))
                  for row in steam.get("games", [])}
        dota = played.get(DOTA_APP_ID, (DOTA_APP_ID, 0, 0))
        if steam.get("dota_match_minutes") and dota[1] == 0:
            played[DOTA_APP_ID] = (DOTA_APP_ID, steam["dota_match_minutes"], dota[2])
        return steam_card_game_ids(played.values())

    def draft_profile(data):
        highlights = draft_steam_highlights(data)
        draft = ProfileInput.model_validate(
            {**data["form"], "game_ids": data.get("selected", [])}, context={"allow_empty_games": bool(highlights)}
        )
        return draft, highlights

    async def review_profile(message, state):
        data = await state.get_data()
        try:
            draft, highlights = draft_profile(data)
        except (KeyError, ValidationError):
            await message.answer("Выбери хотя бы одну игру, чтобы посмотреть анкету.")
            await state.set_state(Form.games)
            await show_games(message, state)
            return
        catalog = registration_catalog(data)
        game_ids = highlights or sorted(draft.game_ids)[:CARD_GAME_LIMIT]
        rank = (data.get("draft_steam") or {}).get("dota_rank_tier")
        games = [dota_game_name(app_id, catalog[app_id].name, rank) for app_id in game_ids if app_id in catalog]
        profile = {**draft.model_dump(), "games": games, "purpose_label": PURPOSES[draft.purpose]}
        wanted = {value: label for label, value in FIELD_STEPS["looking_for"][1]}[draft.looking_for]
        language = {value: label for label, value in FIELD_STEPS["language"][1]}[draft.language]
        filters = f"Кого показывать: {wanted.lower()} · {draft.age_min}–{draft.age_max} лет · {language}"
        await state.update_data(reviewed=True, edit_field=None, edit_draft=False, previous_selected=None)
        await state.set_state(Form.review)
        rows = [[("Опубликовать анкету", "review:publish")],
                [("Изменить", "review:fields"), ("Фото", "review:photo"), ("Описание", "review:bio")]]
        if data.get("draft_steam"):
            rows.append([("Изменить ссылку Steam", "registration:steam")])
        photo = FSInputFile(DEFAULT_AVATAR) if data.get("draft_default_avatar") else data.get("draft_photo_file_id")
        await send_card(message, profile, "<b>Так выглядит твоя анкета</b>\n\n" + profile_text(profile)
                        + f"\n\n{filters}\n\n" + CONTACT_NOTICE
                        + "\nПубликуя анкету, ты соглашаешься на обработку данных для подбора. Подробнее /privacy.",
                        buttons(rows), draft_photo=photo)

    async def display_discovery(message, text, rows, *, profile=None, replace=False):
        markup = buttons(rows)
        photo = card_photo(profile) if profile else None
        if photo and len(text.encode("utf-16-le")) // 2 > 1024:
            await send_card(message, profile, text, markup, draft_photo=photo)
            if replace:
                try:
                    await message.delete()
                except TelegramBadRequest:
                    pass
            return
        if replace:
            try:
                if photo:
                    sent = await message.edit_media(InputMediaPhoto(media=photo, caption=text), reply_markup=markup)
                    if isinstance(photo, FSInputFile) and profile.get("is_test") and isinstance(sent, Message) and sent.photo:
                        service.cache_test_photo(profile["id"], sent.photo[-1].file_id)
                    return
                if message.text is not None:
                    await message.edit_text(text, reply_markup=markup)
                    return
            except TelegramBadRequest as error:
                if "message is not modified" in error.message.lower():
                    return
        sent = None
        if photo:
            try:
                sent = await message.answer_photo(photo, caption=text, reply_markup=markup)
            except TelegramBadRequest:
                log.info("Could not send discovery photo; falling back to text")
            if isinstance(photo, FSInputFile) and profile.get("is_test") and sent and sent.photo:
                service.cache_test_photo(profile["id"], sent.photo[-1].file_id)
        if not sent:
            await message.answer(text, reply_markup=markup)
        if replace:
            try:
                await message.delete()
            except TelegramBadRequest:
                await clear_buttons(message)

    async def clear_buttons(message):
        try:
            await message.edit_reply_markup(reply_markup=None)
        except TelegramBadRequest:
            pass

    def shared_time_text(window):
        if not window:
            return "По указанному расписанию свободные часы не пересекаются."
        day = {0: "сегодня", 1: "завтра"}.get(window["day_offset"], "в ближайшие дни")
        timing = (f"сейчас, до {window['end']}" if window["ongoing"]
                  else f"{day}, {window['start']}–{window['end']}")
        return f"По расписанию вы оба свободны {timing} по твоему времени."

    async def show_feed(message, user, *, replace=False, preferred_id=None, allow_undo=False):
        rows = service.feed(user.id, limit=1, preferred_id=preferred_id)
        focus = service.current_game(user)
        focus_game = next((game.name for game in service.db.catalog() if game.app_id == focus), None) if focus else None
        focus_text = f"🎮 Хочу сыграть в {escape(focus_game)}\n\n" if focus_game else ""
        undo = [("↩ Вернуть предыдущую", "feed:undo")]
        if not rows:
            if not user.active:
                await display_discovery(message, "Анкета на паузе.", [[("Возобновить показы", "feed:resume")]],
                                        replace=replace)
                return
            actions = [
                [("Возраст собеседника", "edit:age_range"), ("Кого показывать", "edit:looking_for")],
                [("Изменить игры", "edit:games"), ("Выбрать игру на сейчас", "feed:focus")],
                undo,
                [("Отменить уведомление", "feed:alert_off") if user.notify_new_profiles
                 else ("Сообщить, когда появится анкета", "feed:alert_on")],
            ]
            if focus:
                actions.insert(0, [("Вернуться к общему поиску", "focus:clear")])
            await display_discovery(message, focus_text + "Новых подходящих анкет пока нет.", actions, replace=replace)
            return
        row = rows[0]
        profile = row["profile"]
        target = profile["id"]
        text = profile_text(profile, common_games=row["common_games"])
        if focus_game:
            heading = f"⚡ Тоже ищет компанию в {escape(focus_game)}" if row["same_game_now"] else focus_text.strip()
            if user.schedule_set and profile["schedule_set"]:
                heading += "\n" + shared_time_text(row["next_overlap"])
            text = f"{heading}\n\n{text}"
        actions = []
        if allow_undo:
            actions.append(undo)
        if focus:
            actions.append([("Вернуться к общему поиску", "focus:clear")])
        if not profile["is_test"]:
            actions.append([("Заблокировать", f"react:block:{target}"), ("Пожаловаться", f"react:report:{target}")])
        actions.append([("💜 Нравится", f"react:like:{target}"), ("👎 Не нравится", f"react:skip:{target}")])
        await display_discovery(message, text, actions, profile=profile, replace=replace)

    async def peer_contact(peer, *, current_username=PHOTO_UNCHANGED):
        if not peer.share_username:
            return "Собеседник ещё не включил обмен контактами."
        username = current_username
        if username is PHOTO_UNCHANGED:
            try:
                username = (await bot.get_chat(peer.telegram_id, request_timeout=5)).username
            except TelegramAPIError:
                return "Контакт пока недоступен. Попробуй обновить список совпадений позже."
        if not username or not re.fullmatch(r"[A-Za-z0-9_]{1,64}", username):
            return "У собеседника пока нет @username."
        return f'Написать в Telegram: <a href="https://t.me/{username}">@{username}</a>'

    async def show_matches(message, user):
        rows = service.matches(user.id)
        if not user.share_username:
            await message.answer(CONTACT_NOTICE, reply_markup=buttons([[("Включить обмен контактами", "match:enable")]]))
        if not rows:
            await message.answer("Здесь появятся взаимные симпатии 💜", reply_markup=menu())
        for row in rows[:30]:
            try:
                peer = service.match_peer(user.id, row["id"])
                contact = await peer_contact(peer)
                service.match_peer(user.id, row["id"])
            except DomainError:
                continue
            await send_card(message, row["profile"], profile_text(row["profile"]) + "\n\n" + contact,
                            reply_markup=buttons([
                [("Заблокировать в боте", f"match:block:{row['id']}"),
                 ("Пожаловаться", f"match:report:{row['id']}")],
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
        text = profile_text(row["profile"], common_games=row["common_games"])
        text += f"\n\nСходство игровых интересов: {row['score']}/100."
        target = row["profile"]["id"]
        await send_card(message,row["profile"],text,reply_markup=buttons([
            [("💜 Тестовый лайк",f"testreact:like:{target}"),("Дальше →",f"testreact:skip:{target}")],
            [("Начать сначала","test:restart")],
        ]))

    @router.message(Command("miniapp"))
    async def open_miniapp(message: Message):
        if not settings.public_url.startswith("https://"):
            await message.answer("Макет доступен в браузере на компьютере, где запущен бот: "
                                 f"{escape(settings.public_url)}/miniapp")
            return
        button = InlineKeyboardButton(text="Открыть PartyUp", web_app=WebAppInfo(url=f"{settings.public_url}/miniapp"))
        await message.answer("Посмотреть тестовые анкеты в Mini App.",
                             reply_markup=InlineKeyboardMarkup(inline_keyboard=[[button]]))

    @router.message(Command("privacy"))
    async def privacy(message: Message):
        await message.answer(
            "<b>Данные и видимость</b>\n"
            "Храним анкету, Telegram ID, username, ссылку Telegram на фото анкеты, предпочтения, выбор игр, реакции, жалобы и события показов. "
            "После подключения Steam — SteamID, доступную библиотеку и игровое время. "
            "Для Dota 2 получаем публичный ранг из OpenDota и оцениваем наигрыш по данным матчей, "
            "если Steam не отдал общий наигрыш. Эти данные используются для подбора.\n\n"
            "В карточке показываем загруженное фото, имя, часовой пояс, описание и до пяти игр. "
            "Если на карточке есть Dota 2 и получен ранг, он отображается рядом с названием. "
            "Людям в подборке также показываем до пяти общих с ними игр, повлиявших на подбор. "
            "Telegram ID и SteamID публично не показываем. С твоего согласия при взаимном лайке бот покажет собеседнику твой @username. "
            "Назначенные операторы и разработчики могут просматривать и исправлять анкеты через закрытую панель; изменения журналируются. "
            "После совпадения общение идёт напрямую в личных сообщениях Telegram.\n\n"
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
            "Подбор по игровой истории: главная общая игра 60%, другие общие игры 30%, жанры 10%. "
            "При равном балле выше анкета с большим пересечением расписания.\n"
            "Отдельно доступна исследовательская модель на Steam Video Games (Tamber, Kaggle, 2017), "
            "ODbL 1.0 / DbCL 1.0.\n"
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
        user = service.update_telegram_account(message.from_user.id, message.from_user.username)
        if user:
            await state.clear()
            await message.answer("Твои люди где-то рядом. Посмотрим, во что играют?", reply_markup=menu())
            return
        current = await state.get_state()
        if current == Form.step.state:
            data = await state.get_data()
            if data.get("edit_draft"):
                await review_profile(message, state)
            else:
                await prompt(message, state)
            return
        if current == Form.games.state:
            data = await state.get_data()
            if not data.get("reviewed") and draft_steam_highlights(data):
                await review_profile(message, state)
            else:
                await show_games(message, state)
            return
        if current == Form.game_source.state:
            await show_game_source(message, state)
            return
        if current == Form.registration_steam.state:
            await prompt_registration_steam(message)
            return
        if current == Form.review.state:
            await review_profile(message, state)
            return
        if current == Form.photo.state:
            data = await state.get_data()
            if data.get("photo_flow") == "review":
                await message.answer("Выбери аватар для анкеты.", reply_markup=avatar_buttons(back="review:open"))
            elif data.get("new_registration"):
                await prompt_registration_avatar(message, state)
            else:
                await message.answer("Пришли фото или нажми «Пропустить».",
                                     reply_markup=buttons([[("Пропустить", "photo:keep")]]))
            return
        await begin_registration(message, state)

    async def begin_registration(message, state):
        await state.set_data({"step_index": 0, "form": {}, "selected": [],
                              "short_registration": True, "new_registration": True})
        await prompt(message, state)

    @router.callback_query(Form.consent, F.data == "consent:yes")
    async def consent(callback, state: FSMContext):
        await callback.answer()
        await clear_buttons(callback.message)
        await begin_registration(callback.message, state)

    @router.message(Command("edit"))
    async def edit(message, state: FSMContext):
        user = await actor(message)
        if user:
            await state.clear()
            await show_edit_menu(message)

    @router.callback_query(F.data.startswith("edit:"))
    async def edit_action(callback, state: FSMContext):
        await callback.answer()
        user = service.user_by_telegram(callback.from_user.id)
        if not user or user.banned:
            return
        await state.clear()
        field = callback.data.split(":")[1]
        message = callback.message
        if field == "menu":
            await show_edit_menu(message)
        elif field == "profile":
            await display_own_profile(message, user)
        elif field == "photo":
            await open_photo(message, state, user)
        elif field == "games":
            await open_games(message, state, user)
        elif field in FIELD_STEPS:
            await state.set_data({"edit_field": field})
            await show_field(message, state, field, back="edit:menu")

    @router.callback_query(StateFilter(Form.review, Form.step, Form.games, Form.photo), F.data.startswith("review:"))
    async def review_action(callback, state: FSMContext):
        await callback.answer()
        data = await state.get_data()
        if not data.get("reviewed"):
            return
        message = callback.message
        field = callback.data.split(":")[1]
        current = await state.get_state()
        if field == "publish" and current != Form.review.state:
            return
        await message.edit_reply_markup(reply_markup=None)
        if field == "open":
            if current == Form.games.state and data.get("previous_selected") is not None:
                await state.update_data(selected=data["previous_selected"])
            await review_profile(message, state)
        elif field == "fields":
            await show_edit_menu(message, draft=True)
        elif field == "photo":
            await state.update_data(photo_flow="review")
            await state.set_state(Form.photo)
            remove = not data.get("new_registration") and bool(data.get("draft_photo_file_id"))
            await message.answer("Выбери аватар для анкеты.",
                                 reply_markup=avatar_buttons(back="review:open", allow_removal=remove))
        elif field == "games":
            await state.update_data(previous_selected=data["selected"])
            await state.set_state(Form.games)
            await show_games(message, state)
        elif field in FIELD_STEPS:
            await state.update_data(edit_field=field, edit_draft=True)
            await show_field(message, state, field, back="review:open")
        elif field == "publish":
            steam_profile = PublicProfile(**data["draft_steam"]) if data.get("draft_steam") else None
            try:
                draft, _ = draft_profile(data)
                schedule_set = "start_hour" in data["form"] if data.get("new_registration") else None
                user_id = service.save_profile(callback.from_user.id, draft, callback.from_user.username,
                                               photo_file_id=data.get("draft_photo_file_id", PHOTO_UNCHANGED),
                                               share_username=True, steam_profile=steam_profile,
                                               use_default_avatar=data.get("draft_default_avatar", False),
                                               schedule_set=schedule_set)
            except (ValidationError, DomainError) as error:
                await message.answer("Не удалось сохранить анкету. " + escape(str(error)[:300]))
                await review_profile(message, state)
                return
            await state.clear()
            extra = "" if callback.from_user.username else "\nЧтобы собеседники могли написать тебе, добавь @username в настройках Telegram."
            published = "Анкета опубликована 💜"
            if steam_profile is None:
                published += " Найди своих или подключи Steam через /steam."
            await message.answer(published + extra, reply_markup=menu())
            if steam_profile:
                await show_feed(message, service.user(user_id))

    @router.message(Command("photo"))
    @router.message(F.text == "📷 Фото анкеты")
    async def edit_photo(message, state: FSMContext):
        user = await actor(message)
        if not user:
            return
        await open_photo(message, state, user)

    async def open_photo(message, state, user):
        await state.set_data({"photo_flow": "profile"})
        await state.set_state(Form.photo)
        await message.answer("Выбери аватар для анкеты.",
                             reply_markup=avatar_buttons(back="edit:menu",
                                                         allow_removal=bool(user.photo_file_id or user.use_default_avatar)))

    @router.message(Form.photo, F.photo)
    async def receive_photo(message, state: FSMContext):
        photo = max(message.photo, key=lambda item: item.width * item.height)
        if photo.file_size is not None and photo.file_size > 10 * 1024 * 1024:
            await message.answer("Фото слишком большое. Отправь его как обычную фотографию до 10 МБ.")
            return
        data = await state.get_data()
        try:
            if data.get("photo_flow") in {"registration", "review"}:
                service.validate_photo(photo.file_id)
            else:
                user = await actor(message)
                if not user:
                    return
                service.set_photo(user.id, photo.file_id)
            await finish_photo(message, state, photo.file_id)
        except DomainError as error:
            await message.answer(escape(str(error)))

    @router.callback_query(Form.photo, F.data.in_({"photo:upload", "photo:default", "photo:keep", "photo:remove"}))
    async def photo_choice(callback, state: FSMContext):
        await callback.answer()
        data = await state.get_data()
        flow = data.get("photo_flow")
        if callback.data == "photo:upload":
            await callback.message.answer("Отправь одну фотографию через скрепку Telegram. Она появится в анкете.",
                                          reply_markup=buttons([[("Выбрать стандартное", "photo:default")]]))
            return
        removed = callback.data == "photo:remove"
        standard = callback.data == "photo:default"
        if data.get("new_registration") and (flow == "registration" and not standard or flow == "review" and removed):
            return
        if (removed or standard) and flow not in {"registration", "review"}:
            user = service.user_by_telegram(callback.from_user.id)
            if not user or user.banned:
                await callback.message.answer("Анкета недоступна.")
                return
            service.set_photo(user.id, None, use_default_avatar=standard)
        await clear_buttons(callback.message)
        await finish_photo(callback.message, state, None if removed or standard else PHOTO_UNCHANGED,
                           use_default_avatar=standard)

    @router.message(Command("games"))
    async def edit_games(message, state: FSMContext):
        user = await actor(message)
        if user:
            await open_games(message, state, user)

    async def open_games(message, state, user):
        await state.set_data({"games_flow": "edit", "selected": [entry.app_id for entry in user.games if entry.selected]})
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
            await display_own_profile(message, user)

    async def display_own_profile(message, user):
        profile = service.public_profile(user)
        await send_card(message, profile, profile_text(profile) + "\n\n/pause — пауза · /delete — удалить",
                        reply_markup=buttons([[("Изменить анкету", "edit:menu")],
                                              [("Фото", "edit:photo"), ("Игры", "edit:games")]]))

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
            user = service.update_telegram_account(message.from_user.id, message.from_user.username)
            await show_matches(message, user)

    @router.callback_query(F.data.startswith("match:"))
    async def match_action(callback, state: FSMContext):
        await callback.answer()
        user = service.user_by_telegram(callback.from_user.id)
        if not user or user.banned:
            return
        _, action, *args = callback.data.split(":")
        if action == "enable":
            user = service.update_telegram_account(callback.from_user.id, callback.from_user.username,
                                                   share_username=True)
            await clear_buttons(callback.message)
            if not user.username:
                await callback.message.answer("У тебя пока нет @username. Добавь его в Telegram и снова открой «Совпадения».")
            await show_matches(callback.message, user)
        elif action == "list":
            user = service.update_telegram_account(callback.from_user.id, callback.from_user.username)
            await show_matches(callback.message, user)
        elif action in {"block", "report"}:
            try:
                peer = service.match_peer(user.id, int(args[0]))
                service.react(user.id, peer.id, action)
            except (DomainError, IndexError, ValueError) as error:
                await callback.message.answer(escape(str(error)))
                return
            await clear_buttons(callback.message)
            text = "Контакт заблокирован в боте." if action == "block" else "Жалоба отправлена. Контакт заблокирован в боте."
            await callback.message.answer(text + " Если вы уже переписываетесь в Telegram, заблокируй собеседника и там.")

    async def show_focus(message, query=""):
        catalog = service.db.catalog()
        needle = query.casefold()
        games = [game for game in catalog if any(needle in name.casefold() for name in [game.name, *game.aliases])][:12]
        rows = [[(game.name[:55], f"focus:game:{game.app_id}")] for game in games]
        rows.append([("Общий поиск", "focus:clear")])
        text = "В какую игру хочешь сыграть? Выбор действует четыре часа. Можешь написать название."
        if not games:
            text = "Игра не найдена. Попробуй другое название."
        await message.answer(text, reply_markup=buttons(rows))

    @router.message(Command("play"))
    @router.message(F.text == "🎮 Хочу сыграть сейчас")
    async def focus_menu(message, state: FSMContext):
        if await actor(message):
            await state.clear()
            await state.set_state(Form.focus)
            await show_focus(message)

    @router.message(Command("alerts"))
    async def alerts(message):
        user = await actor(message)
        if user:
            text = "Уведомление о новой подходящей анкете включено." if user.notify_new_profiles else "Уведомление о новых анкетах выключено."
            rows = [[("Отключить", "alert:off")]] if user.notify_new_profiles else []
            await message.answer(text, reply_markup=buttons(rows) if rows else None)

    @router.callback_query(F.data == "alert:off")
    async def disable_alert(callback):
        user = service.user_by_telegram(callback.from_user.id)
        if user:
            service.set_profile_alert(user.id, False)
        await callback.answer("Уведомление отключено")
        await clear_buttons(callback.message)

    @router.callback_query(F.data.startswith("focus:") | F.data.startswith("feed:"))
    async def discovery_action(callback, state: FSMContext):
        user = service.user_by_telegram(callback.from_user.id)
        if not user or user.banned:
            await callback.answer("Анкета недоступна.")
            return
        scope, action, *args = callback.data.split(":")
        message = callback.message
        if action == "undo":
            target = service.undo_skip(user.id)
            if target is None:
                await callback.answer("Можно вернуть только последний пропуск.", show_alert=True)
                return
            await callback.answer("Пропуск отменён")
            if service.feed(user.id, limit=1, record_impressions=False, preferred_id=target):
                await show_feed(message, user, replace=True, preferred_id=target)
            else:
                await message.answer("Анкета больше не подходит под текущий поиск или недоступна.")
                await show_feed(message, user, replace=True)
            return
        await callback.answer()
        if scope == "focus":
            if action == "game" and await state.get_state() != Form.focus.state:
                return
            if action not in {"game", "clear"}:
                return
            service.set_current_game(user.id, int(args[0]) if action == "game" else None)
            await state.clear()
            await show_feed(message, service.user(user.id), replace=True)
        elif action == "focus":
            await state.clear()
            await state.set_state(Form.focus)
            await show_focus(message)
        elif action in {"alert_on", "alert_off"}:
            enabled = action == "alert_on"
            service.set_profile_alert(user.id, enabled)
            await message.answer("Сообщу один раз, когда появится подходящая анкета." if enabled else "Уведомление отключено.")
            await show_feed(message, service.user(user.id), replace=True)
        elif action == "resume":
            if not user.active:
                service.toggle_active(user.id)
            await show_feed(message, service.user(user.id), replace=True)

    @router.message(Command("test"))
    async def test_profiles(message,state:FSMContext):
        user = await actor(message)
        if not user:
            return
        await state.clear()
        await message.answer(f"🧪 Тестовых анкет: {len(fixture_definitions())}. "
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
                             "https://steamcommunity.com/profiles/SteamID. Получим доступные открытые игры "
                             "и публичную статистику Dota 2 из OpenDota. "
                             "Если Steam требует входа или скрывает библиотеку, выбрать игры можно через /games. "
                             "Пароль и API-ключ не нужны. /cancel — вернуться.",
                             reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))

    async def fetch_steam_profile(value):
        if steam is not None:
            return await steam.fetch_profile(value, public_steam)
        if public_steam is not None:
            return await public_steam.fetch(value)
        raise DomainError("Парсер Steam временно недоступен")

    async def import_public(message, user, value, *, refresh=False):
        profile = await fetch_steam_profile(value)
        service.save_public_steam(user.id, profile, expected_steam_id=user.steam_id if refresh else None)
        saved = service.public_profile(service.user(user.id))
        text = "Ссылка Steam сохранена.\n" + steam_status_text(saved)
        if profile.warning:
            text += "\n" + profile.warning
        if saved["dota_match_estimate"]:
            text += "\nДля Dota 2 учтена оценка наигрыша по данным OpenDota."
        text += "\n/profile — посмотреть анкету. /games — выбрать актуальные игры."
        await message.answer(escape(text), reply_markup=menu())

    async def steam_attempt_allowed(message, state):
        data = await state.get_data()
        if data.get("steam_attempt_at") and (utcnow() - datetime.fromisoformat(data["steam_attempt_at"])).total_seconds() < 10:
            await message.answer("Подожди несколько секунд перед следующим запросом Steam.")
            return False
        await state.update_data(steam_attempt_at=utcnow().isoformat())
        return True

    @router.callback_query(StateFilter(Form.game_source, Form.games, Form.review), F.data == "registration:steam")
    async def registration_steam(callback, state: FSMContext):
        await callback.answer()
        data = await state.get_data()
        if not data.get("short_registration") or data.get("games_flow") == "edit":
            return
        await clear_buttons(callback.message)
        await state.set_state(Form.registration_steam)
        await prompt_registration_steam(callback.message)

    @router.callback_query(StateFilter(Form.registration_steam, Form.games),
                           F.data.in_({"registration:source", "registration:games"}))
    async def registration_source(callback, state: FSMContext):
        await callback.answer()
        data = await state.get_data()
        if not data.get("short_registration") or data.get("games_flow") == "edit":
            return
        await clear_buttons(callback.message)
        if data.get("reviewed"):
            await review_profile(callback.message, state)
        else:
            await show_game_source(callback.message, state)

    @router.callback_query(StateFilter(Form.game_source, Form.registration_steam), F.data == "registration:manual")
    async def registration_games(callback, state: FSMContext):
        await callback.answer()
        await clear_buttons(callback.message)
        await state.update_data(draft_steam=None)
        await state.set_state(Form.games)
        await show_games(callback.message, state)

    @router.message(Form.registration_steam, F.text)
    async def registration_steam_link(message, state: FSMContext):
        if not await steam_attempt_allowed(message, state):
            return
        try:
            profile = await fetch_steam_profile(message.text)
        except DomainError as error:
            await message.answer(escape(str(error)),
                                 reply_markup=buttons([[("Выбрать игры из списка", "registration:manual")]]))
            return
        data = await state.get_data()
        previous_ids = {row["appid"] for row in (data.get("draft_steam") or {}).get("games", [])}
        selected = [app_id for app_id in data.get("selected", []) if app_id not in previous_ids]
        data = await state.update_data(draft_steam=asdict(profile), selected=selected)
        has_playtime = bool(draft_steam_highlights(data))
        if profile.coverage == "profile_only":
            status = "Ссылка Steam принята. Steam не предоставил список игр и часы."
        else:
            status = (f"Из библиотеки Steam получено игр: {len(profile.games)}."
                      if profile.coverage == "library" and profile.games
                      else steam_status_text({"steam_import_status": profile.coverage,
                                              "steam_game_count": len(profile.games)}))
        if profile.dota_match_minutes:
            status += " Для Dota 2 учтена оценка наигрыша по данным OpenDota."
        status += (" Бот сам выбрал игры для карточки." if has_playtime
                   else " Можно продолжить с играми, выбранными вручную.")
        await message.answer(status)
        if has_playtime:
            await review_profile(message, state)
        else:
            await state.set_state(Form.games)
            await show_games(message, state)

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
            await import_public(callback.message, user, "https://steamcommunity.com/profiles/" + user.steam_id,
                                refresh=True)
            await state.clear()
        except DomainError as error:
            await callback.message.answer(escape(str(error)))

    @router.message(Form.steam_link, F.text)
    async def steam_link_message(message, state: FSMContext):
        user = await actor(message)
        if not user:
            return
        if not await steam_attempt_allowed(message, state):
            return
        try:
            await import_public(message, user, message.text)
            await state.clear()
        except DomainError as error:
            await message.answer(escape(str(error)))

    @router.callback_query(F.data.regexp(r"^(?:react|testreact):(like|skip|block|report):\d+$"))
    async def react(callback):
        await callback.answer()
        user = service.update_telegram_account(callback.from_user.id, callback.from_user.username)
        if not user or user.banned:
            return
        origin, kind, target = callback.data.split(":")
        peer = service.user(int(target))
        if not peer or (origin == "testreact" and not peer.is_test):
            await callback.message.answer("Анкета больше недоступна.")
            return
        if kind == "like" and not peer.is_test and not user.share_username:
            await callback.message.answer(
                CONTACT_NOTICE + " После подтверждения нажми «Нравится» ещё раз.",
                reply_markup=buttons([[("Включить обмен контактами", "match:enable")]]),
            )
            return
        try:
            result = service.react(user.id, peer.id, kind, fresh=True, gallery=origin == "testreact")
        except DomainError as error:
            await callback.message.answer(escape(str(error)))
            return
        if origin == "testreact":
            await clear_buttons(callback.message)
            if kind == "like":
                await callback.message.answer("Тестовый лайк сохранён ✓")
            await show_test_feed(callback.message,service.user(user.id))
            return
        if kind == "report":
            await callback.message.answer("Жалоба отправлена в очередь модерации. Анкета также заблокирована для тебя.")
        if result["matched"] and result["new"]:
            mine = await peer_contact(user, current_username=callback.from_user.username)
            theirs = await peer_contact(peer)
            try:
                service.match_peer(user.id, result["match_id"])
            except DomainError:
                await show_feed(callback.message, service.user(user.id), replace=True)
                return
            markup = buttons([[("Открыть совпадения", "match:list")]])
            hint = "" if callback.from_user.username else "\nДля обмена добавь @username в настройках Telegram."
            await callback.message.answer(f"💜 Взаимная симпатия с {escape(peer.name)}!\n{theirs}" + hint,
                                          reply_markup=markup)
            try:
                service.match_peer(peer.id, result["match_id"])
                await bot.send_message(peer.telegram_id, f"💜 Взаимная симпатия с {escape(user.name)}!\n{mine}",
                                       reply_markup=markup)
            except DomainError:
                pass
            except TelegramAPIError as error:
                log.info("Could not deliver a match notification: %s", type(error).__name__)
        await show_feed(callback.message, service.user(user.id), replace=True, allow_undo=kind == "skip")

    @router.callback_query(F.data.startswith("chat:") | F.data.startswith("contact:"))
    async def old_chat_button(callback, state: FSMContext):
        await callback.answer()
        await state.clear()
        user = service.update_telegram_account(callback.from_user.id, callback.from_user.username)
        if user and not user.banned:
            await show_matches(callback.message, user)

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
        await consume_step(message, state, message.from_user, message.text)

    @router.callback_query(StateFilter(Form.step, Form.games, Form.game_source, Form.photo), F.data == "form:back")
    async def back_step(callback, state: FSMContext):
        await callback.answer()
        data = await state.get_data()
        if data.get("edit_field") or data.get("games_flow") == "edit":
            return
        current = await state.get_state()
        if current == Form.photo.state and data.get("photo_flow") != "registration":
            return
        if current == Form.game_source.state and data.get("new_registration"):
            await clear_buttons(callback.message)
            await prompt_registration_avatar(callback.message, state)
            return
        await state.update_data(step_index=max(0, data.get("step_index", 0) - 1))
        await clear_buttons(callback.message)
        await prompt(callback.message, state)

    @router.callback_query(Form.step, F.data.startswith("form:"))
    async def step_button(callback, state: FSMContext):
        await callback.answer()
        parts = callback.data.split(":", 2)
        expected = parts[1] if len(parts) == 3 else None
        await consume_step(callback.message, state, callback.from_user, parts[-1], expected)
        await clear_buttons(callback.message)

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
            registration = data.get("games_flow") != "edit" and not service.user_by_telegram(callback.from_user.id)
            if registration:
                await clear_buttons(callback.message)
                await review_profile(callback.message, state)
                return
            try:
                service.save_profile(callback.from_user.id, None, callback.from_user.username,
                                     changes={"game_ids": selected})
            except (ValidationError, DomainError) as error:
                await callback.message.answer("Не удалось сохранить. Выбери 1–10 игр и проверь анкету. " + escape(str(error)[:300]))
                return
            await state.clear()
            await callback.message.edit_reply_markup(reply_markup=None)
            await callback.message.answer("Актуальные игры сохранены.", reply_markup=menu())
            return
        if not value.isascii() or not value.isdecimal():
            return
        app_id = int(value)
        if app_id not in registration_catalog(data):
            return
        if app_id in selected:
            selected.remove(app_id)
        elif len(selected) < 10:
            selected.append(app_id)
        await state.update_data(selected=selected)
        await callback.message.edit_reply_markup(reply_markup=None)
        await show_games(callback.message, state)

    @router.message(Form.focus, F.text)
    async def focus_search(message, state: FSMContext):
        await show_focus(message, message.text.strip())

    @router.message(Form.photo)
    async def unsupported_photo(message):
        await message.answer("Нужно обычное фото: скрепка → Фото или видео. "
                             "Изображения файлом, видео и ссылки пока не подходят. "
                             "Можно выбрать стандартный аватар в меню или выйти через /cancel.")

    @router.message(F.photo)
    async def photo_hint(message):
        await message.answer("Добавить или заменить фото анкеты можно через /photo. "
                             "При регистрации бот предложит выбрать аватар перед подключением Steam.")

    @router.message()
    async def fallback(message):
        await message.answer("Используй меню или /start. /cancel — выйти из заполнения анкеты.", reply_markup=menu())

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


async def deliver_profile_alerts(bot, service):
    for user_id, telegram_id in service.pending_profile_alerts():
        user = service.user(user_id)
        if not user or not user.notify_new_profiles:
            continue
        if not service.feed(user_id, record_impressions=False, real_only=True, availability_only=True):
            continue
        try:
            await bot.send_message(telegram_id, "В твоём поиске появилась подходящая анкета. Открыть подборку — /find.")
        except TelegramForbiddenError:
            pass
        except TelegramAPIError as error:
            log.info("Could not deliver a discovery notification: %s", type(error).__name__)
            continue
        service.set_profile_alert(user_id, False)


async def profile_alerts(bot, service):
    while True:
        await asyncio.sleep(90)
        await deliver_profile_alerts(bot, service)


async def run_polling(bot, dispatcher, settings=None, service=None):
    commands=[
        BotCommand(command="find", description="Найти своих"),
        BotCommand(command="miniapp", description="Открыть макет Mini App"),
        BotCommand(command="play", description="Найти компанию для игры сейчас"),
        BotCommand(command="profile", description="Моя анкета"),
        BotCommand(command="photo", description="Добавить или изменить фото анкеты"),
        BotCommand(command="matches", description="Взаимные симпатии"),
        BotCommand(command="steam", description="Подключить Steam"),
        BotCommand(command="games", description="Выбрать актуальные игры"),
        BotCommand(command="test", description="Тестовые анкеты с фотографиями"),
        BotCommand(command="edit", description="Изменить общие настройки"),
        BotCommand(command="pause", description="Пауза или возобновление"),
        BotCommand(command="privacy", description="Данные и приватность"),
        BotCommand(command="alerts", description="Уведомление о новых анкетах"),
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
    alerts_task = asyncio.create_task(profile_alerts(bot, service)) if service else None
    try:
        await dispatcher.start_polling(bot, handle_signals=False, close_bot_session=False,
                                       allowed_updates=dispatcher.resolve_used_update_types())
    finally:
        if alerts_task:
            alerts_task.cancel()
            await asyncio.gather(alerts_task, return_exceptions=True)
