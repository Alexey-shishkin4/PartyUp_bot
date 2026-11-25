"""HTTP entry points for the Telegram Mini App and its separate fixture preview."""
import json
import time
from asyncio import to_thread, wait_for
from collections import OrderedDict
from contextlib import asynccontextmanager
from functools import lru_cache
from io import BytesIO

from aiogram.exceptions import TelegramAPIError
from aiogram.types import BufferedInputFile
from aiogram.utils.web_app import WebAppUser, safe_parse_webapp_init_data
from aiohttp import web
from PIL import Image, ImageOps
from pydantic import ValidationError
from sqlalchemy import select

from .config import RESOURCES, ROOT
from .domain.errors import DomainError
from .domain.profiles import ProfileInput
from .domain.matching import steam_card_game_ids
from .persistence.models import Event
from .services.fixture_catalog import fixture_definitions, fixture_photo
from .telegram.contacts import current_username
from .telegram.ui import MAX_PHOTO_SIZE, USERNAME_REQUIRED

STATIC = ROOT / "static/miniapp"
DECK = ("mira", "nika", "mark", "asya", "artem", "sonya")


def thumbnail(data):
    with Image.open(BytesIO(data)) as source:
        if source.width * source.height > 36_000_000:
            raise ValueError("Photo dimensions exceed the thumbnail limit")
        source.draft("RGB", (540, 540))
        image = ImageOps.exif_transpose(source).convert("RGB")
        image.thumbnail((540, 540), Image.Resampling.LANCZOS)
        result = BytesIO()
        image.save(result, "WEBP", quality=72)
        return result.getvalue()


@lru_cache(maxsize=1)
def deck():
    catalog = {game["app_id"]: game["name"] for game in
               json.loads((RESOURCES / "catalog.json").read_text(encoding="utf-8"))}
    fixtures = {row["slug"]: row for row in fixture_definitions().values()}
    cards = []
    for slug in DECK:
        row = fixtures[slug]
        library = row.get("library", [])
        ranked = steam_card_game_ids((g["app_id"], g["hours"] * 60, g.get("recent_hours", 0) * 60)
                                     for g in library)
        names = {g["app_id"]: g["name"] for g in library if g.get("name")}
        games = [names.get(i) or catalog.get(i, f"Steam {i}")
                 for i in dict.fromkeys([*ranked, *row.get("game_ids", [])])]
        cards.append({**{k: row[k] for k in ("slug", "name", "age", "timezone", "bio", "start_hour", "end_hour")},
                      "photo": f"/miniapp/portrait/{slug}", "games": games[:5],
                      "incoming_like": slug in ("mira", "mark")})
    return cards


async def portrait(request):
    slug = request.match_info["slug"]
    if slug in DECK:
        for identifier, row in fixture_definitions().items():
            if row["slug"] == slug and (photo := fixture_photo(identifier)):
                return web.FileResponse(photo)
    raise web.HTTPNotFound()


def authenticate(request, bot):
    scheme, _, raw = request.headers.get("Authorization", "").partition(" ")
    try:
        if scheme != "tma" or not raw:
            raise ValueError
        data = safe_parse_webapp_init_data(bot.token, raw)
        if not data.user or data.user.id <= 0 or not -30 <= time.time() - data.auth_date.timestamp() <= 3600:
            raise ValueError
        return data.user
    except (ValueError, KeyError, OverflowError, OSError):
        raise web.HTTPUnauthorized(text="Открой Mini App заново через Telegram.") from None


@web.middleware
async def headers(request, handler):
    try:
        response = await handler(request)
    except DomainError as error:
        response = web.Response(status=409, text=str(error))
    except ValidationError:
        response = web.Response(status=400, text="Проверь имя (2–40 символов), возраст (18–99), часовой пояс и игры (до 10).")
    except web.HTTPException as error:
        response = web.Response(status=error.status, text=error.text)
    response.headers.update({
        "Referrer-Policy": "no-referrer", "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": "default-src 'self'; script-src 'self' https://telegram.org; "
        "img-src 'self' blob:; style-src 'self'; connect-src 'self'; base-uri 'none'; "
        "frame-ancestors https://web.telegram.org https://*.web.telegram.org",
    })
    response.headers.setdefault("Cache-Control", "no-store")
    return response


def create_web_app(services=None, bot=None, *, debug=None, test_feed_telegram_id=None):
    @web.middleware
    async def local_only(request, handler):
        if request.remote != "127.0.0.1" or request.headers.get("Host") != f"127.0.0.1:{debug.miniapp_port}":
            raise web.HTTPForbidden()
        if any(key in request.headers for key in ("Forwarded", "X-Forwarded-Host", "X-Forwarded-For", "X-Forwarded-Proto")):
            raise web.HTTPForbidden()
        origin = request.headers.get("Origin")
        if origin and origin != f"http://127.0.0.1:{debug.miniapp_port}":
            raise web.HTTPForbidden()
        response = await handler(request)
        response.headers["X-Frame-Options"] = "DENY"
        return response

    app = web.Application(middlewares=[headers, local_only] if debug else [headers], client_max_size=16 * 1024)

    async def page(request):
        demo = request.path == "/miniapp/demo"
        html = (STATIC / "index.html").read_text(encoding="utf-8")
        sdk = '' if demo or debug else '<script defer src="https://telegram.org/js/telegram-web-app.js"></script>'
        mode = "Демо · 18+" if demo else "Debug · 18+" if debug else "18+"
        return web.Response(text=html.replace("<!-- SDK -->", sdk)
                            .replace("{{entry}}", "app" if demo else "live")
                            .replace("{{local_debug}}", "true" if debug and not demo else "false")
                            .replace("{{mode}}", mode), content_type="text/html")

    async def demo_deck(request):
        return web.json_response(deck())

    async def avatar(request):
        return web.FileResponse(STATIC / "avatar.webp", headers={"Content-Type": "image/webp"})

    async def brand_art(request):
        return web.FileResponse(STATIC / "partyup.webp", headers={"Content-Type": "image/webp"})

    async def intro(request):
        return web.FileResponse(STATIC / "rose.webp", headers={
            "Content-Type": "image/webp", "Cache-Control": "public, max-age=86400",
        })

    app.router.add_get("/miniapp", page)
    app.router.add_get("/miniapp/", page)
    app.router.add_get("/miniapp/demo", page)
    app.router.add_get("/miniapp/deck", demo_deck)
    app.router.add_get("/miniapp/portrait/{slug}", portrait)
    app.router.add_get("/miniapp/avatar", avatar)
    app.router.add_get("/miniapp/art", brand_art)
    app.router.add_get("/miniapp/intro", intro)
    app.router.add_static("/static/miniapp", STATIC)
    if services is None:
        return app

    profiles, discovery = services.profiles, services.discovery
    photo_cache = OrderedDict()

    def viewer(request, *, allow_new=False):
        if debug:
            if request.headers.get("X-PartyUp-Debug") != "1":
                raise web.HTTPUnauthorized()
            telegram_id = debug.miniapp_debug_telegram_id
            user = profiles.user_by_telegram(telegram_id)
            account = WebAppUser(id=telegram_id, first_name=user.name if user else "Игрок",
                                 username=user.username if user else None)
        else:
            account = authenticate(request, bot)
            user = profiles.user_by_telegram(account.id)
        if not user and not allow_new:
            raise web.HTTPForbidden(text="Сначала создай анкету командой /start в боте.")
        if user and (user.banned or user.is_test):
            raise web.HTTPForbidden(text="Анкета недоступна.")
        return user, account

    async def body(request):
        try:
            data = await request.json()
        except (ValueError, UnicodeError):
            raise web.HTTPBadRequest(text="Некорректный JSON.") from None
        if not isinstance(data, dict):
            raise web.HTTPBadRequest(text="Ожидался объект.")
        return data

    def card(row):
        return {key: row[key] for key in ("id", "name", "age", "bio", "timezone", "start_hour", "end_hour",
                                           "schedule_set", "games", "additional_games", "is_test", "has_photo")}

    def profile_payload(user, account):
        catalog = [{"id": g.app_id, "name": g.name} for g in profiles.catalog()]
        if user is None:
            return {"registered": False, "name": account.first_name, "catalog": catalog}
        own = profiles.public_profile(user)
        return {**card(own), "registered": True, "purpose": user.purpose, "purpose_label": own["purpose_label"],
                "game_ids": own["game_ids"], "active": user.active,
                "steam_connected": own["steam_connected"], "catalog": catalog}

    async def me(request):
        return web.json_response(profile_payload(*viewer(request, allow_new=True)))

    def test_feed(user):
        return user.telegram_id == test_feed_telegram_id

    def discover(user, **options):
        gallery = test_feed(user)
        return discovery.feed(user.id, test_only=gallery, allow_test_profiles=gallery, **options)

    def feed_cards(user, **options):
        return [{**card(r["profile"]), "common_games": r["common_games"]}
                for r in discover(user, limit=1, **options)]

    def match_cards(user_id):
        return [{"id": m["id"], "profile": card(m["profile"])} for m in discovery.matches(user_id)]

    async def bootstrap(request):
        user, account = viewer(request, allow_new=True)
        return web.json_response({
            "profile": profile_payload(user, account),
            "feed": feed_cards(user) if user else [],
            "matches": match_cards(user.id) if user else [],
            "can_undo": discovery.can_undo_skip(user.id, test_only=test_feed(user)) if user else False,
        })

    async def edit(request):
        creating = request.method == "POST"
        user, account = viewer(request, allow_new=creating)
        data = await body(request)
        base = {"name", "bio", "game_ids"}
        if not base <= data.keys():
            raise web.HTTPBadRequest(text="Проверь поля анкеты.")
        if data.pop("share_username", True) is not True:
            raise web.HTTPBadRequest(text="Открой Mini App заново. Условия обмена контактами обновились.")
        if creating and data.pop("consent", False) is not True:
            raise web.HTTPBadRequest(text="Подтверди публикацию анкеты.")
        if data.keys() - {"name", "age", "timezone", "purpose", "bio", "game_ids"}:
            raise web.HTTPBadRequest(text="Неизвестное поле анкеты.")
        if creating:
            profile = ProfileInput.model_validate(data)
            profiles.save_profile(account.id, profile, username=account.username, share_username=True,
                                  photo_file_id=None, use_default_avatar=True, schedule_set=False, create_only=True)
        else:
            profiles.save_profile(user.telegram_id, None, username=account.username, changes=data)
        return web.json_response({"saved": True})

    async def update_photo(request):
        user, _ = viewer(request)
        if request.method == "DELETE":
            profiles.set_photo(user.id, None, use_default_avatar=True)
        else:
            if request.content_type not in {"image/jpeg", "image/png"}:
                raise web.HTTPBadRequest(text="Выбери фотографию JPEG или PNG.")
            upload = await request.clone(client_max_size=MAX_PHOTO_SIZE + 1).read()
            if not upload or len(upload) > MAX_PHOTO_SIZE:
                raise web.HTTPBadRequest(text="Можно загрузить фото до 10 МБ.")
            try:
                msg = await bot.send_photo(user.telegram_id, BufferedInputFile(upload, filename="avatar"),
                                           caption="Фото для твоей анкеты PartyUp")
            except (TelegramAPIError, TimeoutError):
                raise web.HTTPBadRequest(text="Telegram не принял фото. Проверь доступ бота к чату и формат файла.") from None
            profiles.set_photo(user.id, msg.photo[-1].file_id)
        return web.json_response({"saved": True})

    async def feed(request):
        user, _ = viewer(request)
        can_undo = discovery.can_undo_skip(user.id, test_only=test_feed(user))
        return web.json_response(feed_cards(user), headers={"X-Can-Undo": "1" if can_undo else "0"})

    async def react(request):
        user, account = viewer(request)
        data = await body(request)
        if type(data.get("target")) is not int or not 0 < data["target"] < 2**31 or data.get("kind") not in ("like", "skip"):
            raise web.HTTPBadRequest(text="Некорректная реакция.")
        if data["kind"] == "like":
            peer = profiles.user(data["target"])
            if peer and not peer.is_test and not account.username:
                raise DomainError(USERNAME_REQUIRED)
        gallery = test_feed(user)
        result = discovery.react(user.id, data["target"], data["kind"], fresh=True,
                                 gallery=gallery, allow_test_profiles=gallery)
        if result["new"]:
            message = "💜 У тебя новая взаимность! Открой /matches."
            try:
                username = await current_username(bot, user)
                if username:
                    message = f'💜 Взаимная симпатия! Написать в Telegram: <a href="https://t.me/{username}">@{username}</a>'
            except (TelegramAPIError, TimeoutError):
                pass
            try:
                peer = discovery.match_peer(user.id, result["match_id"])
                await bot.send_message(peer.telegram_id, message, request_timeout=5)
            except (DomainError, TelegramAPIError, TimeoutError):
                pass  # The committed match is still available to both users.
        return web.json_response({"matched": result["matched"],
                                  **({"match_id": result["match_id"]} if result["matched"] else {})})

    async def undo(request):
        user, _ = viewer(request)
        target = discovery.undo_skip(user.id, test_only=test_feed(user))
        rows = feed_cards(user, preferred_id=target) if target else []
        return web.json_response({"undone": target is not None, "card": rows[0] if rows else None})

    async def matches(request):
        user, _ = viewer(request)
        return web.json_response(match_cards(user.id))

    async def contact(request):
        user, _ = viewer(request)
        match_id = int(request.match_info["id"])
        peer = discovery.match_peer(user.id, match_id)
        try:
            username = await current_username(bot, peer)
        except (TelegramAPIError, TimeoutError):
            raise web.HTTPServiceUnavailable(text="Telegram пока не ответил. Попробуй ещё раз.") from None
        discovery.match_peer(user.id, match_id)
        return web.json_response({"username": username})

    async def photo(request):
        user, _ = viewer(request)
        target_id = int(request.match_info["id"])

        def allowed():
            if target_id == user.id:
                return True
            # Serve only previously shown cards that are still eligible for this viewer.
            with services.db.session() as session:
                shown = session.scalar(select(Event.id).where(
                    Event.actor_id == user.id, Event.target_id == target_id,
                    Event.kind.in_(("impression", "test_impression")),
                ).limit(1))
            return shown is not None and discover(
                user, preferred_id=target_id, availability_only=True, record_impressions=False)

        if not allowed():
            raise web.HTTPNotFound()
        fixture = profiles.test_photo_file(target_id)
        if fixture:
            return web.FileResponse(fixture)
        file_id = profiles.photo_file(target_id)
        if file_id:
            if file_id in photo_cache:
                photo_cache.move_to_end(file_id)
                return web.Response(body=photo_cache[file_id], content_type="image/webp")
            try:
                result = await wait_for(bot.download(file_id, timeout=8), timeout=8)
                if result is not None:
                    image = await to_thread(thumbnail, result.read())
                    if not allowed() or profiles.photo_file(target_id) != file_id:
                        raise web.HTTPNotFound()
                    if len(image) <= 256 * 1024:
                        photo_cache[file_id] = image
                        photo_cache.move_to_end(file_id)
                        if len(photo_cache) > 32:
                            photo_cache.popitem(last=False)
                    return web.Response(body=image, content_type="image/webp")
            except (TelegramAPIError, TimeoutError, Image.DecompressionBombError, OSError, ValueError):
                pass
            raise web.HTTPServiceUnavailable(text="Фото пока недоступно.")
        return await avatar(request)

    app.add_routes([
        web.get("/miniapp/api/bootstrap", bootstrap),
        web.get("/miniapp/api/me", me), web.put("/miniapp/api/me", edit), web.post("/miniapp/api/me", edit),
        web.put("/miniapp/api/me/photo", update_photo), web.delete("/miniapp/api/me/photo", update_photo),
        web.get("/miniapp/api/feed", feed), web.post("/miniapp/api/react", react),
        web.post("/miniapp/api/undo", undo), web.get("/miniapp/api/matches", matches),
        web.get(r"/miniapp/api/contact/{id:[0-9]{1,10}}", contact),
        web.get(r"/miniapp/api/photo/{id:[0-9]{1,10}}", photo),
    ])
    return app


@asynccontextmanager
async def miniapp_server(settings, services, bot):
    if not settings.miniapp_url and not settings.miniapp_debug:
        yield
        return
    debug = settings if settings.miniapp_debug else None
    test_id = settings.miniapp_test_feed_telegram_id
    runner = web.AppRunner(
        create_web_app(services, bot, debug=debug, test_feed_telegram_id=test_id if test_id in settings.developers else None),
        access_log=None,
    )
    await runner.setup()
    try:
        host = "127.0.0.1" if debug else "0.0.0.0"
        await web.TCPSite(runner, host, settings.miniapp_port).start()
        yield
    finally:
        await runner.cleanup()


if __name__ == "__main__":
    web.run_app(create_web_app(), host="127.0.0.1", port=8871, access_log=None)
