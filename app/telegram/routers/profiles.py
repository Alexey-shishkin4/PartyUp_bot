from dataclasses import asdict
from html import escape

from aiogram import F, Router
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from pydantic import ValidationError

from app.domain.errors import DomainError
from app.domain.profiles import PHOTO_UNCHANGED
from app.domain.steam import PublicProfile
from app.telegram.profile_form import FIELD_STEPS, ProfileForm
from app.telegram.states import Form
from app.telegram.steam_ui import steam_attempt_allowed, steam_status_text
from app.telegram.ui import MAX_PHOTO_SIZE, avatar_buttons, buttons, clear_buttons, menu, require_profile


def build_profile_router(profiles, cards, fetch_steam_profile, feed_view):
    router = Router(name="profiles")
    workflow = ProfileForm(profiles, cards)

    @router.message(CommandStart())
    async def start(message, state: FSMContext):
        user = profiles.update_telegram_account(message.from_user.id, message.from_user.username)
        if user:
            await state.clear()
            await message.answer("Твои люди где-то рядом. Посмотрим, во что играют?", reply_markup=menu())
            return
        current = await state.get_state()
        if current == Form.step.state:
            data = await state.get_data()
            if data.get("edit_draft"):
                await workflow.review_profile(message, state)
            else:
                await workflow.prompt(message, state)
            return
        if current == Form.games.state:
            data = await state.get_data()
            if not data.get("reviewed") and workflow.draft_steam_highlights(data):
                await workflow.review_profile(message, state)
            else:
                await workflow.show_games(message, state)
            return
        if current == Form.game_source.state:
            await workflow.show_game_source(message, state)
            return
        if current == Form.registration_steam.state:
            await workflow.prompt_registration_steam(message)
            return
        if current == Form.review.state:
            await workflow.review_profile(message, state)
            return
        if current == Form.photo.state:
            data = await state.get_data()
            if data.get("photo_flow") == "review":
                await message.answer(
                    "Выбери аватар для анкеты.", reply_markup=avatar_buttons(back="review:open")
                )
            elif data.get("new_registration"):
                await workflow.prompt_registration_avatar(message, state)
            else:
                await message.answer(
                    "Пришли фото или нажми «Пропустить».",
                    reply_markup=buttons([[("Пропустить", "photo:keep")]]),
                )
            return
        await begin_registration(message, state)

    async def begin_registration(message, state):
        await state.set_data(
            {
                "step_index": 0,
                "form": {},
                "selected": [],
                "short_registration": True,
                "new_registration": True,
            }
        )
        await workflow.prompt(message, state)

    @router.callback_query(Form.consent, F.data == "consent:yes")
    async def consent(callback, state: FSMContext):
        await callback.answer()
        await clear_buttons(callback.message)
        await begin_registration(callback.message, state)

    @router.message(Command("edit"))
    async def edit(message, state: FSMContext):
        user = await require_profile(profiles, message)
        if user:
            await state.clear()
            await workflow.show_edit_menu(message)

    @router.callback_query(F.data.startswith("edit:"))
    async def edit_action(callback, state: FSMContext):
        await callback.answer()
        user = profiles.user_by_telegram(callback.from_user.id)
        if not user or user.banned:
            return
        await state.clear()
        field = callback.data.split(":")[1]
        message = callback.message
        if field == "menu":
            await workflow.show_edit_menu(message)
        elif field == "profile":
            await workflow.display_own_profile(message, user)
        elif field == "photo":
            await workflow.open_photo(message, state, user)
        elif field == "games":
            await workflow.open_games(message, state, user)
        elif field in FIELD_STEPS:
            await state.set_data({"edit_field": field})
            await workflow.show_field(message, state, field, back="edit:menu")

    @router.callback_query(
        StateFilter(Form.review, Form.step, Form.games, Form.photo), F.data.startswith("review:")
    )
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
            await workflow.review_profile(message, state)
        elif field == "fields":
            await workflow.show_edit_menu(message, draft=True)
        elif field == "photo":
            await state.update_data(photo_flow="review")
            await state.set_state(Form.photo)
            remove = not data.get("new_registration") and bool(data.get("draft_photo_file_id"))
            await message.answer(
                "Выбери аватар для анкеты.",
                reply_markup=avatar_buttons(back="review:open", allow_removal=remove),
            )
        elif field == "games":
            await state.update_data(previous_selected=data["selected"])
            await state.set_state(Form.games)
            await workflow.show_games(message, state)
        elif field in FIELD_STEPS:
            await state.update_data(edit_field=field, edit_draft=True)
            await workflow.show_field(message, state, field, back="review:open")
        elif field == "publish":
            steam_profile = PublicProfile(**data["draft_steam"]) if data.get("draft_steam") else None
            try:
                draft, _ = workflow.draft_profile(data)
                schedule_set = "start_hour" in data["form"] if data.get("new_registration") else None
                user_id = profiles.save_profile(
                    callback.from_user.id,
                    draft,
                    callback.from_user.username,
                    photo_file_id=data.get("draft_photo_file_id", PHOTO_UNCHANGED),
                    share_username=True,
                    steam_profile=steam_profile,
                    use_default_avatar=data.get("draft_default_avatar", False),
                    schedule_set=schedule_set,
                    create_only=True,
                )
            except (ValidationError, DomainError) as error:
                await message.answer("Не удалось сохранить анкету. " + escape(str(error)[:300]))
                await workflow.review_profile(message, state)
                return
            await state.clear()
            extra = (
                ""
                if callback.from_user.username
                else "\nЧтобы собеседники могли написать тебе, добавь @username в настройках Telegram."
            )
            published = "Анкета опубликована 💜"
            if steam_profile is None:
                published += " Найди своих или подключи Steam через /steam."
            await message.answer(published + extra, reply_markup=menu())
            if steam_profile:
                await feed_view.show(message, profiles.user(user_id))

    @router.message(Command("photo"))
    @router.message(F.text == "📷 Фото анкеты")
    async def edit_photo(message, state: FSMContext):
        user = await require_profile(profiles, message)
        if not user:
            return
        await workflow.open_photo(message, state, user)

    @router.message(Form.photo, F.photo)
    async def receive_photo(message, state: FSMContext):
        photo = max(message.photo, key=lambda item: item.width * item.height)
        if photo.file_size is not None and photo.file_size > MAX_PHOTO_SIZE:
            await message.answer("Фото слишком большое. Отправь его как обычную фотографию до 10 МБ.")
            return
        data = await state.get_data()
        try:
            if data.get("photo_flow") in {"registration", "review"}:
                profiles.validate_photo(photo.file_id)
            else:
                user = await require_profile(profiles, message)
                if not user:
                    return
                profiles.set_photo(user.id, photo.file_id)
            await workflow.finish_photo(message, state, photo.file_id)
        except DomainError as error:
            await message.answer(escape(str(error)))

    @router.callback_query(
        Form.photo, F.data.in_({"photo:upload", "photo:default", "photo:keep", "photo:remove"})
    )
    async def photo_choice(callback, state: FSMContext):
        await callback.answer()
        data = await state.get_data()
        flow = data.get("photo_flow")
        if callback.data == "photo:upload":
            await callback.message.answer(
                "Отправь одну фотографию через скрепку Telegram. Она появится в анкете.",
                reply_markup=buttons([[("Выбрать стандартное", "photo:default")]]),
            )
            return
        removed = callback.data == "photo:remove"
        standard = callback.data == "photo:default"
        if data.get("new_registration") and (
            flow == "registration" and not standard or flow == "review" and removed
        ):
            return
        if (removed or standard) and flow not in {"registration", "review"}:
            user = profiles.user_by_telegram(callback.from_user.id)
            if not user or user.banned:
                await callback.message.answer("Анкета недоступна.")
                return
            profiles.set_photo(user.id, None, use_default_avatar=standard)
        await clear_buttons(callback.message)
        await workflow.finish_photo(
            callback.message,
            state,
            None if removed or standard else PHOTO_UNCHANGED,
            use_default_avatar=standard,
        )

    @router.message(Command("games"))
    async def edit_games(message, state: FSMContext):
        user = await require_profile(profiles, message)
        if user:
            await workflow.open_games(message, state, user)

    @router.message(Command("pause"))
    async def pause(message):
        user = await require_profile(profiles, message)
        if user:
            active = profiles.toggle_active(user.id)
            await message.answer(
                "Анкета снова в поиске." if active else "Анкета на паузе. Возобновить: /pause"
            )

    @router.message(Command("profile"))
    @router.message(F.text == "👤 Моя анкета")
    async def own_profile(message):
        user = await require_profile(profiles, message)
        if user:
            await workflow.display_own_profile(message, user)

    @router.callback_query(
        StateFilter(Form.game_source, Form.games, Form.review), F.data == "registration:steam"
    )
    async def registration_steam(callback, state: FSMContext):
        await callback.answer()
        data = await state.get_data()
        if not data.get("short_registration") or data.get("games_flow") == "edit":
            return
        await clear_buttons(callback.message)
        await state.set_state(Form.registration_steam)
        await workflow.prompt_registration_steam(callback.message)

    @router.callback_query(
        StateFilter(Form.registration_steam, Form.games),
        F.data.in_({"registration:source", "registration:games"}),
    )
    async def registration_source(callback, state: FSMContext):
        await callback.answer()
        data = await state.get_data()
        if not data.get("short_registration") or data.get("games_flow") == "edit":
            return
        await clear_buttons(callback.message)
        if data.get("reviewed"):
            await workflow.review_profile(callback.message, state)
        else:
            await workflow.show_game_source(callback.message, state)

    @router.callback_query(
        StateFilter(Form.game_source, Form.registration_steam), F.data == "registration:manual"
    )
    async def registration_games(callback, state: FSMContext):
        await callback.answer()
        await clear_buttons(callback.message)
        await state.update_data(draft_steam=None)
        await state.set_state(Form.games)
        await workflow.show_games(callback.message, state)

    @router.message(Form.registration_steam, F.text, ~F.text.startswith("/"))
    async def registration_steam_link(message, state: FSMContext):
        if not await steam_attempt_allowed(message, state):
            return
        try:
            profile = await fetch_steam_profile(message.text)
        except DomainError as error:
            await message.answer(
                escape(str(error)),
                reply_markup=buttons([[("Выбрать игры из списка", "registration:manual")]]),
            )
            return
        data = await state.get_data()
        previous_ids = {row["appid"] for row in (data.get("draft_steam") or {}).get("games", [])}
        selected = [app_id for app_id in data.get("selected", []) if app_id not in previous_ids]
        data = await state.update_data(draft_steam=asdict(profile), selected=selected)
        has_playtime = bool(workflow.draft_steam_highlights(data))
        if profile.coverage == "profile_only":
            status = "Ссылка Steam принята. Steam не предоставил список игр и часы."
        else:
            status = (
                f"Из библиотеки Steam получено игр: {len(profile.games)}."
                if profile.coverage == "library" and profile.games
                else steam_status_text(
                    {"steam_import_status": profile.coverage, "steam_game_count": len(profile.games)}
                )
            )
        if profile.dota_match_minutes:
            status += " Для Dota 2 учтена оценка наигрыша по данным OpenDota."
        status += (
            " Бот сам выбрал игры для карточки."
            if has_playtime
            else " Можно продолжить с играми, выбранными вручную."
        )
        await message.answer(status)
        if has_playtime:
            await workflow.review_profile(message, state)
        else:
            await state.set_state(Form.games)
            await workflow.show_games(message, state)

    @router.message(Form.step, F.text, ~F.text.startswith("/"))
    async def step_text(message, state: FSMContext):
        await workflow.consume_step(message, state, message.from_user, message.text)

    @router.callback_query(
        StateFilter(Form.step, Form.games, Form.game_source, Form.photo), F.data == "form:back"
    )
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
            await workflow.prompt_registration_avatar(callback.message, state)
            return
        await state.update_data(step_index=max(0, data.get("step_index", 0) - 1))
        await clear_buttons(callback.message)
        await workflow.prompt(callback.message, state)

    @router.callback_query(Form.step, F.data.startswith("form:"))
    async def step_button(callback, state: FSMContext):
        await callback.answer()
        parts = callback.data.split(":", 2)
        expected = parts[1] if len(parts) == 3 else None
        await workflow.consume_step(callback.message, state, callback.from_user, parts[-1], expected)
        await clear_buttons(callback.message)

    @router.message(Form.games, F.text, ~F.text.startswith("/"))
    async def game_search(message, state: FSMContext):
        await workflow.show_games(message, state, message.text.strip())

    @router.callback_query(Form.games, F.data.startswith("game:"))
    async def game_button(callback, state: FSMContext):
        await callback.answer()
        data = await state.get_data()
        selected = list(data.get("selected", []))
        value = callback.data.split(":")[1]
        if value == "save":
            registration = data.get("games_flow") != "edit" and not profiles.user_by_telegram(
                callback.from_user.id
            )
            if registration:
                await clear_buttons(callback.message)
                await workflow.review_profile(callback.message, state)
                return
            try:
                profiles.save_profile(
                    callback.from_user.id, None, callback.from_user.username, changes={"game_ids": selected}
                )
            except (ValidationError, DomainError) as error:
                await callback.message.answer(
                    "Не удалось сохранить. Выбери 1–10 игр и проверь анкету. " + escape(str(error)[:300])
                )
                return
            await state.clear()
            await callback.message.edit_reply_markup(reply_markup=None)
            await callback.message.answer("Актуальные игры сохранены.", reply_markup=menu())
            return
        if not value.isascii() or not value.isdecimal():
            return
        app_id = int(value)
        if app_id not in workflow.registration_catalog(data):
            return
        if app_id in selected:
            selected.remove(app_id)
        elif len(selected) < 10:
            selected.append(app_id)
        await state.update_data(selected=selected)
        await callback.message.edit_reply_markup(reply_markup=None)
        await workflow.show_games(callback.message, state)

    return router
