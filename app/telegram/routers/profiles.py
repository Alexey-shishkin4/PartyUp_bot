from dataclasses import asdict
from html import escape

from aiogram import F, Router
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from pydantic import ValidationError

from app.domain.errors import DomainError
from app.domain.faceit import FaceitLookup
from app.domain.game_links_draft import GameLinksDraft
from app.domain.profiles import PHOTO_UNCHANGED
from app.domain.steam import PublicProfile
from app.telegram.profile_form import FIELD_STEPS, ProfileForm
from app.telegram.states import Form
from app.telegram.steam_ui import steam_attempt_allowed, steam_status_text
from app.telegram.ui import MAX_PHOTO_SIZE, avatar_buttons, buttons, clear_buttons, menu, require_profile

from .game_accounts import PROMPTS


def build_profile_router(profiles, cards, fetch_steam_profile, game_flow):
    router = Router(name="profiles")
    accounts = game_flow.accounts
    workflow = ProfileForm(profiles, cards, accounts.draft_providers)

    async def prompt_draft_account(message, data):
        name = data["draft_provider"]
        rows = [[("← Назад", "draftgame:back")]]
        if getattr(GameLinksDraft.from_state(data), name) is not None:
            rows.insert(0, [("Убрать из черновика", f"draftgame:remove:{name}")])
        if name in {"faceit", "deadlock"} and data.get("draft_steam"):
            rows.insert(0, [("Использовать Steam", f"draftgame:steam:{name}")])
        prompt = PROMPTS[name] if name != "deadlock" else "Загрузим ранг Deadlock из Steam, добавленного в черновик."
        await message.answer(
            prompt + "\n\nПривязка сохранится в анкете после публикации.",
            reply_markup=buttons(rows),
        )

    @router.message(CommandStart())
    async def start(message, state: FSMContext):
        user = profiles.update_telegram_account(message.from_user.id, message.from_user.username)
        if user:
            await state.clear()
            await message.answer("Твои люди где-то рядом. Посмотрим, во что играют?", reply_markup=menu())
            return
        data = await state.get_data()
        if not data.get("new_registration"):
            await begin_registration(message, state)
            return
        current = await state.get_state()
        if current == Form.step.state:
            if data.get("edit_draft"):
                await workflow.review_profile(message, state)
            else:
                await workflow.prompt(message, state)
            return
        if current == Form.games.state:
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
        if current == Form.registration_game_account.state:
            if data.get("draft_provider") in accounts.draft_providers:
                await prompt_draft_account(message, data)
            else:
                await workflow.review_profile(message, state)
            return
        if current == Form.review.state:
            await workflow.review_profile(message, state)
            return
        if current == Form.photo.state:
            if data.get("photo_flow") == "review":
                await message.answer(
                    "Выбери аватар для анкеты.", reply_markup=avatar_buttons(back="review:open")
                )
            else:
                await workflow.prompt_registration_avatar(message, state)
            return
        await begin_registration(message, state)

    async def begin_registration(message, state):
        await state.set_data(
            {
                "step_index": 0,
                "form": {},
                "selected": [],
                "new_registration": True,
            }
        )
        await workflow.prompt(message, state)

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
        if not data.get("new_registration") or not data.get("reviewed"):
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
            await message.answer(
                "Выбери аватар для анкеты.",
                reply_markup=avatar_buttons(back="review:open"),
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
                links = GameLinksDraft.from_state(data)
                draft, _ = workflow.draft_profile(data, links)
                user_id = profiles.save_profile(
                    callback.from_user.id,
                    draft,
                    callback.from_user.username,
                    photo_file_id=data.get("draft_photo_file_id", PHOTO_UNCHANGED),
                    share_username=True,
                    steam_profile=steam_profile,
                    game_links_draft=links,
                    use_default_avatar=data.get("draft_default_avatar", False),
                    schedule_set="start_hour" in data["form"],
                    allow_empty_games=data.get("skip_games", False),
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
            await message.answer("Анкета опубликована 💜" + extra, reply_markup=menu())
            await game_flow.begin(message, state, profiles.user(user_id))

    @router.callback_query(
        StateFilter(Form.game_source, Form.review),
        F.data.in_({f"draftgame:{name}" for name in accounts.draft_providers}),
    )
    async def open_draft_account(callback, state: FSMContext):
        await callback.answer()
        if not (await state.get_data()).get("new_registration"):
            return
        from_game_source = await state.get_state() == Form.game_source.state
        name = callback.data.split(":")[1]
        await clear_buttons(callback.message)
        data = await state.update_data(
            draft_provider=name, draft_from_game_source=from_game_source,
        )
        if name == "deadlock" and not data.get("draft_steam"):
            await state.update_data(draft_check_deadlock=True)
            await state.set_state(Form.registration_steam)
            await callback.message.answer("Для Deadlock понадобится ссылка на Steam.")
            await workflow.prompt_registration_steam(callback.message)
            return
        await state.set_state(Form.registration_game_account)
        await prompt_draft_account(callback.message, data)

    async def load_draft_account(message, state, name, identity, data):
        if not await game_flow.attempt(message, state, name):
            return
        links = GameLinksDraft.from_state(data)
        try:
            missing = await accounts.link_draft(links, name, identity)
        except DomainError as error:
            await message.answer(escape(str(error)))
            await prompt_draft_account(message, data)
            return
        await state.update_data(draft_game_links=links.model_dump(mode="json"))
        if missing:
            await message.answer("Пока не удалось получить данные для " + ", ".join(missing) + ".")
        await workflow.review_profile(message, state)

    @router.callback_query(Form.registration_game_account, F.data.startswith("draftgame:"))
    async def draft_account_action(callback, state: FSMContext):
        await callback.answer()
        data = await state.get_data()
        name = data.get("draft_provider")
        steam = data.get("draft_steam")
        show_source = data.get("draft_from_game_source")
        if steam and name in {"faceit", "deadlock"} and callback.data == f"draftgame:steam:{name}":
            steam_id = steam["steam_id"]
            identity = FaceitLookup("steam_id", steam_id) if name == "faceit" else steam_id
            await load_draft_account(callback.message, state, name, identity, data)
            return
        if name in accounts.draft_providers and callback.data == f"draftgame:remove:{name}":
            links = GameLinksDraft.from_state(data)
            setattr(links, name, None)
            await state.update_data(draft_game_links=links.model_dump(mode="json"))
            if not (links.has_links or data.get("selected") or data.get("skip_games")
                    or workflow.draft_steam_highlights(data)):
                show_source = True
        elif callback.data != "draftgame:back":
            return
        await clear_buttons(callback.message)
        if show_source:
            await workflow.show_game_source(callback.message, state)
        else:
            await workflow.review_profile(callback.message, state)

    @router.message(Form.registration_game_account, F.text, ~F.text.startswith("/"))
    async def draft_account_identity(message, state: FSMContext):
        data = await state.get_data()
        name = data.get("draft_provider")
        if name not in accounts.draft_providers:
            await workflow.review_profile(message, state)
            return
        if name == "deadlock":
            await prompt_draft_account(message, data)
            return
        try:
            identity = accounts.parse_identity(name, message.text)
        except DomainError as error:
            await message.answer(escape(str(error)))
            await prompt_draft_account(message, data)
            return
        await load_draft_account(message, state, name, identity, data)

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
        Form.photo, F.data.in_({"photo:upload", "photo:default", "photo:remove"})
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
        if removed and flow in {"registration", "review"}:
            return
        if flow not in {"registration", "review"}:
            user = profiles.user_by_telegram(callback.from_user.id)
            if not user or user.banned:
                await callback.message.answer("Анкета недоступна.")
                return
            profiles.set_photo(user.id, None, use_default_avatar=standard)
        await clear_buttons(callback.message)
        await workflow.finish_photo(callback.message, state, None, use_default_avatar=standard)

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
        if not data.get("new_registration"):
            return
        await clear_buttons(callback.message)
        await state.update_data(draft_check_deadlock=False)
        await state.set_state(Form.registration_steam)
        await workflow.prompt_registration_steam(callback.message)

    @router.callback_query(
        StateFilter(Form.registration_steam, Form.games),
        F.data == "registration:source",
    )
    async def registration_source(callback, state: FSMContext):
        await callback.answer()
        data = await state.get_data()
        if not data.get("new_registration"):
            return
        await clear_buttons(callback.message)
        await state.update_data(draft_check_deadlock=False)
        if data.get("reviewed"):
            await workflow.review_profile(callback.message, state)
        else:
            await workflow.show_game_source(callback.message, state)

    @router.callback_query(
        StateFilter(Form.game_source, Form.registration_steam), F.data == "registration:manual"
    )
    async def registration_games(callback, state: FSMContext):
        await callback.answer()
        data = await state.get_data()
        if not data.get("new_registration"):
            return
        await clear_buttons(callback.message)
        links = GameLinksDraft.from_state(data)
        if data.get("draft_steam"):
            links.clear_steam_links(data["draft_steam"]["steam_id"])
        await state.update_data(
            draft_steam=None, draft_game_links=links.model_dump(mode="json"),
            draft_check_deadlock=False,
        )
        await state.set_state(Form.games)
        await workflow.show_games(callback.message, state)

    @router.callback_query(
        StateFilter(Form.game_source, Form.registration_steam, Form.games), F.data == "registration:skip"
    )
    async def skip_registration_games(callback, state: FSMContext):
        await callback.answer()
        data = await state.get_data()
        if data.get("new_registration"):
            await clear_buttons(callback.message)
            await state.update_data(skip_games=True, draft_check_deadlock=False)
            await workflow.review_profile(callback.message, state)

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
        previous = data.get("draft_steam") or {}
        links = GameLinksDraft.from_state(data)
        if previous.get("steam_id") and previous["steam_id"] != profile.steam_id:
            links.clear_steam_links(previous["steam_id"])
        missing = await accounts.sync_draft_steam(links, profile, check_deadlock=data.get("draft_check_deadlock", False))
        previous_ids = {row["appid"] for row in previous.get("games", [])}
        selected = [app_id for app_id in data.get("selected", []) if app_id not in previous_ids]
        data = await state.update_data(
            draft_steam=asdict(profile), selected=selected,
            draft_game_links=links.model_dump(mode="json"), draft_check_deadlock=False,
        )
        has_steam_games = bool(workflow.draft_steam_highlights(data))
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
            if has_steam_games
            else " Можно продолжить с играми, выбранными вручную."
        )
        if missing:
            status += "\nПока не удалось получить данные для " + ", ".join(missing) + "."
        await message.answer(status)
        if has_steam_games or links.has_links:
            await workflow.review_profile(message, state)
        else:
            await state.set_state(Form.games)
            await workflow.show_games(message, state)

    @router.message(Form.step, F.text, ~F.text.startswith("/"))
    async def step_text(message, state: FSMContext):
        await workflow.consume_step(message, state, message.from_user, message.text)

    @router.callback_query(
        StateFilter(Form.step, Form.game_source, Form.photo), F.data == "form:back"
    )
    async def back_step(callback, state: FSMContext):
        await callback.answer()
        data = await state.get_data()
        if not data.get("new_registration") or data.get("edit_field"):
            return
        current = await state.get_state()
        if current == Form.photo.state and data.get("photo_flow") != "registration":
            return
        if current == Form.game_source.state:
            await clear_buttons(callback.message)
            await workflow.prompt_registration_avatar(callback.message, state)
            return
        await state.update_data(step_index=max(0, data.get("step_index", 0) - 1))
        await clear_buttons(callback.message)
        await workflow.prompt(callback.message, state)

    @router.callback_query(Form.step, F.data.regexp(r"^form:[a-z_]+:.+$"))
    async def step_button(callback, state: FSMContext):
        await callback.answer()
        _, expected, value = callback.data.split(":", 2)
        await workflow.consume_step(callback.message, state, callback.from_user, value, expected)
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
            registration = data.get("new_registration") and not profiles.user_by_telegram(
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
