import re
from html import escape

from aiogram.types import FSInputFile
from pydantic import ValidationError

from app.domain.dota import DOTA_APP_ID, dota_game_name
from app.domain.errors import DomainError
from app.domain.matching import CARD_GAME_LIMIT, steam_card_game_ids
from app.domain.profiles import PHOTO_UNCHANGED, PURPOSES, ProfileInput
from app.persistence.models import Game

from .cards import DEFAULT_AVATAR, profile_text
from .states import Form
from .ui import CONTACT_NOTICE, avatar_buttons, buttons, menu

STEPS = [
    ("name", "Как тебя показывать? Имя или ник, от 2 до 40 символов.", None),
    ("age", "Сколько тебе лет? Бот для совершеннолетних: 18–99.", None),
    (
        "gender",
        "Как указать твой пол?",
        [("Мужской", "male"), ("Женский", "female"), ("Другой / не указывать", "other")],
    ),
    (
        "looking_for",
        "Кого показывать в общей ленте? Одна настройка для всех знакомств.",
        [("Всех", "any"), ("Мужчин", "male"), ("Женщин", "female"), ("Другой / не указан", "other")],
    ),
    (
        "purpose",
        "Чего ждёшь от знакомства?",
        [("Друзей и тиммейтов", "friends"), ("Отношений", "romance"), ("Обоих вариантов", "both")],
    ),
    ("age_range", "Желаемый возраст собеседника, например 20-30. Можно 18-99.", None),
    ("language", "На каком языке хочешь общаться?", [("Русский", "ru"), ("English", "en")]),
    (
        "timezone",
        "Твой часовой пояс. Можно выбрать или написать, например Asia/Novosibirsk.",
        [
            ("Москва · UTC+3", "Europe/Moscow"),
            ("Екатеринбург · UTC+5", "Asia/Yekaterinburg"),
            ("Новосибирск · UTC+7", "Asia/Novosibirsk"),
            ("Владивосток · UTC+10", "Asia/Vladivostok"),
        ],
    ),
    (
        "hours",
        "Когда обычно играешь по своему времени? Выбери или напиши свой интервал, например 21-23.",
        [("Днём · 12–17", "12-17"), ("Вечером · 19–23", "19-23"), ("Ночью · 22–02", "22-2")],
    ),
    ("bio", "Расскажи о себе и стиле игры. До 500 символов.", [("Без описания", "-")]),
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


class ProfileForm:
    def __init__(self, profiles, cards):
        self.profiles = profiles
        self.cards = cards

    async def prompt(self, message, state):
        data = await state.get_data()
        index = data["step_index"]
        fields = registration_fields(data)
        if index >= len(fields) and data.get("new_registration"):
            await self.prompt_registration_avatar(message, state)
            return
        if index >= len(fields) and data.get("short_registration"):
            await self.show_game_source(message, state)
            return
        if index >= len(fields):
            await state.update_data(photo_flow="registration")
            await state.set_state(Form.photo)
            await message.answer(
                f"<b>{len(STEPS) + 1}/{len(STEPS) + 2}</b> · Добавь фото анкеты.\n"
                "Отправь одну фотографию обычным фото через скрепку Telegram. "
                "Она будет видна людям в подборке. Можно продолжить без нового фото.",
                reply_markup=buttons([[("Продолжить без нового фото", "photo:keep")]]),
            )
            return
        await self.show_field(
            message,
            state,
            fields[index],
            prefix=f"<b>{index + 1}/{len(fields) + 1}</b> · ",
            back="form:back" if index > 0 else None,
        )

    async def prompt_registration_avatar(self, message, state):
        await state.update_data(photo_flow="registration")
        await state.set_state(Form.photo)
        await message.answer("Выбери аватар для анкеты.", reply_markup=avatar_buttons(back="form:back"))

    async def show_field(self, message, state, field, *, prefix="", back=None):
        prompt_text, choices = FIELD_STEPS[field]
        rows = [[(label, f"form:{field}:{value}")] for label, value in choices or []]
        if back:
            rows.append([("← Назад", back)])
        await state.set_state(Form.step)
        await message.answer(prefix + prompt_text, reply_markup=buttons(rows) if rows else None)

    async def consume_step(self, message, state, sender, value, expected_field=None):
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
            if (
                choices
                and field not in {"hours", "bio", "timezone"}
                and value not in {choice[1] for choice in choices}
            ):
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
                    if (
                        not (0 <= left <= 23 and 0 <= right <= 24)
                        or left == right
                        or (left == 0 and right == 24)
                    ):
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
                self.profiles.save_profile(sender.id, None, sender.username, changes=form)
        except (ValueError, DomainError) as error:
            await message.answer(escape(str(error)))
            return
        if data.get("edit_field") and not data.get("edit_draft"):
            await state.clear()
            await self.show_edit_menu(message, prefix="Сохранено ✓\n")
            return
        form = {**data.get("form", {}), **form}
        if data.get("edit_draft"):
            await state.update_data(form=form, edit_field=None, edit_draft=False)
            await self.review_profile(message, state)
            return
        await state.update_data(form=form, step_index=index + 1)
        await self.prompt(message, state)

    def registration_catalog(self, data):
        catalog = {game.app_id: game for game in self.profiles.catalog()}
        for row in (data.get("draft_steam") or {}).get("games", []):
            app_id = row["appid"]
            if app_id not in catalog:
                catalog[app_id] = Game(app_id=app_id, name=row.get("name", f"Steam {app_id}"), aliases=[])
        return catalog

    async def show_game_source(self, message, state):
        await state.set_state(Form.game_source)
        await message.answer(
            "Подключи Steam, чтобы бот сам выбрал игры для анкеты и учёл всю доступную библиотеку в подборе.",
            reply_markup=buttons(
                [
                    [("🎮 Подключить Steam", "registration:steam")],
                    [("Выбрать игры из списка", "registration:manual")],
                    [("← Назад", "form:back")],
                ]
            ),
        )

    async def show_games(self, message, state, query=""):
        data = await state.get_data()
        selected = data.get("selected", [])
        catalog = self.registration_catalog(data)
        query = query.casefold()
        games = sorted(catalog.values(), key=lambda game: (game.app_id not in selected, game.name.casefold()))
        games = [
            game for game in games if any(query in text.casefold() for text in [game.name, *game.aliases])
        ][:12]
        registration = data.get("games_flow") != "edit"
        rows = [
            [(f"{'✓ ' if game.app_id in selected else ''}{game.name[:60]}", f"game:{game.app_id}")]
            for game in games
        ]
        rows.append([("Посмотреть анкету" if registration else "Сохранить игры", "game:save")])
        if data.get("short_registration"):
            rows.append([("← Назад", "review:open" if data.get("reviewed") else "registration:source")])
        elif not registration:
            rows.append([("← К настройкам", "edit:menu")])
        names = [catalog[app_id].name[:55] for app_id in selected if app_id in catalog]
        await message.answer(
            f"Выбрано {len(selected)}/10: {escape(', '.join(names)) or 'пока ничего'}\n"
            "Выбери актуальные игры кнопками или отправь название для поиска.",
            reply_markup=buttons(rows),
        )

    async def prompt_registration_steam(self, message):
        await message.answer(
            "Пришли ссылку на свой профиль steamcommunity.com. Получим доступные игры и часы для подбора, "
            "а для Dota 2 проверим публичную статистику в OpenDota.",
            reply_markup=buttons([[("← К выбору способа", "registration:source")]]),
        )

    async def finish_photo(self, message, state, file_id=PHOTO_UNCHANGED, *, use_default_avatar=False):
        data = await state.get_data()
        if data.get("photo_flow") in {"registration", "review"}:
            if file_id is not PHOTO_UNCHANGED:
                await state.update_data(draft_photo_file_id=file_id, draft_default_avatar=use_default_avatar)
            if data.get("photo_flow") == "review":
                await self.review_profile(message, state)
                return
            if data.get("new_registration"):
                await self.show_game_source(message, state)
                return
            await state.set_state(Form.games)
            await message.answer(
                "Выбери от 1 до 10 актуальных игр. Напиши название для поиска или выбирай кнопками. Steam можно подключить после сохранения."
            )
            await self.show_games(message, state)
        else:
            await state.clear()
            text = (
                "Стандартный аватар установлен."
                if use_default_avatar
                else "Вернулись в меню."
                if file_id is PHOTO_UNCHANGED
                else "Фото удалено из анкеты."
                if file_id is None
                else "Фото сохранено."
            )
            await message.answer(text, reply_markup=menu())

    async def show_edit_menu(self, message, *, prefix="", draft=False):
        space = "review" if draft else "edit"
        rows = [[(label, f"{space}:{field}") for label, field in row] for row in EDIT_FIELDS]
        rows.extend([[("Игры", f"{space}:games"), ("Фото", f"{space}:photo")]])
        if draft:
            rows.append([("← К предпросмотру", "review:open")])
        else:
            rows.append([("← Моя анкета", "edit:profile")])
        await message.answer(prefix + "Что хочешь изменить?", reply_markup=buttons(rows))

    def draft_steam_highlights(self, data):
        steam = data.get("draft_steam") or {}
        played = {
            row["appid"]: (row["appid"], row.get("playtime_forever", 0), row.get("playtime_2weeks", 0))
            for row in steam.get("games", [])
        }
        dota = played.get(DOTA_APP_ID, (DOTA_APP_ID, 0, 0))
        if steam.get("dota_match_minutes") and dota[1] == 0:
            played[DOTA_APP_ID] = (DOTA_APP_ID, steam["dota_match_minutes"], dota[2])
        return steam_card_game_ids(played.values())

    def draft_profile(self, data):
        highlights = self.draft_steam_highlights(data)
        draft = ProfileInput.model_validate(
            {**data["form"], "game_ids": data.get("selected", [])},
            context={"allow_empty_games": bool(highlights)},
        )
        return draft, highlights

    async def review_profile(self, message, state):
        data = await state.get_data()
        try:
            draft, highlights = self.draft_profile(data)
        except (KeyError, ValidationError):
            await message.answer("Выбери хотя бы одну игру, чтобы посмотреть анкету.")
            await state.set_state(Form.games)
            await self.show_games(message, state)
            return
        catalog = self.registration_catalog(data)
        game_ids = highlights or sorted(draft.game_ids)[:CARD_GAME_LIMIT]
        rank = (data.get("draft_steam") or {}).get("dota_rank_tier")
        games = [
            dota_game_name(app_id, catalog[app_id].name, rank) for app_id in game_ids if app_id in catalog
        ]
        profile = {**draft.model_dump(), "games": games, "purpose_label": PURPOSES[draft.purpose]}
        profile["additional_games"] = [catalog[i].name for i in sorted(draft.game_ids) if i in catalog and i not in game_ids]
        wanted = {value: label for label, value in FIELD_STEPS["looking_for"][1]}[draft.looking_for]
        language = {value: label for label, value in FIELD_STEPS["language"][1]}[draft.language]
        filters = f"Кого показывать: {wanted.lower()} · {draft.age_min}–{draft.age_max} лет · {language}"
        await state.update_data(reviewed=True, edit_field=None, edit_draft=False, previous_selected=None)
        await state.set_state(Form.review)
        rows = [
            [("Опубликовать анкету", "review:publish")],
            [("Изменить", "review:fields"), ("Фото", "review:photo"), ("Описание", "review:bio")],
        ]
        if data.get("draft_steam"):
            rows.append([("Изменить ссылку Steam", "registration:steam")])
        photo = (
            FSInputFile(DEFAULT_AVATAR)
            if data.get("draft_default_avatar")
            else data.get("draft_photo_file_id")
        )
        await self.cards.send(
            message,
            profile,
            "<b>Так выглядит твоя анкета</b>\n\n"
            + profile_text(profile)
            + f"\n\n{filters}\n\n"
            + CONTACT_NOTICE
            + "\nПубликуя анкету, ты соглашаешься на обработку данных для подбора. Подробнее /privacy.",
            buttons(rows),
            draft_photo=photo,
        )

    async def open_photo(self, message, state, user):
        await state.set_data({"photo_flow": "profile"})
        await state.set_state(Form.photo)
        await message.answer(
            "Выбери аватар для анкеты.",
            reply_markup=avatar_buttons(
                back="edit:menu", allow_removal=bool(user.photo_file_id or user.use_default_avatar)
            ),
        )

    async def open_games(self, message, state, user):
        await state.set_data(
            {"games_flow": "edit", "selected": [entry.app_id for entry in user.games if entry.selected]}
        )
        await state.set_state(Form.games)
        await self.show_games(message, state)

    async def display_own_profile(self, message, user):
        profile = self.profiles.public_profile(user)
        await self.cards.send(
            message,
            profile,
            profile_text(profile) + "\n\n/pause — пауза · /delete — удалить",
            reply_markup=buttons(
                [[("Изменить анкету", "edit:menu")], [("Фото", "edit:photo"), ("Игры", "edit:games")]]
            ),
        )
