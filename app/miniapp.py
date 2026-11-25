"""HTTP entry points for the Telegram Mini App and its separate fixture preview."""
import json
import time
from contextlib import asynccontextmanager
from functools import lru_cache

from aiogram.exceptions import TelegramAPIError
from aiogram.utils.web_app import safe_parse_webapp_init_data
from aiohttp import web
from pydantic import ValidationError
from sqlalchemy import select

from .config import RESOURCES, ROOT
from .domain.errors import DomainError
from .domain.matching import steam_card_game_ids
from .persistence.models import Event
from .services.fixture_catalog import fixture_definitions, fixture_photo
from .telegram.contacts import current_username

STATIC = ROOT / "static/miniapp"
DECK = ("mira", "nika", "mark", "asya", "artem", "sonya")


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
        response = web.Response(status=400, text="Проверь имя, описание и выбранные игры.")
    except web.HTTPException as error:
        response = web.Response(status=error.status, text=error.text)
    response.headers.update({
        "Cache-Control": "no-store", "Referrer-Policy": "no-referrer", "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": "default-src 'self'; script-src 'self' https://telegram.org; "
        "img-src 'self' blob:; style-src 'self'; connect-src 'self'; base-uri 'none'; "
        "frame-ancestors https://web.telegram.org https://*.web.telegram.org",
    })
    return response


def create_web_app(services=None, bot=None):
    app = web.Application(middlewares=[headers], client_max_size=16 * 1024)

    async def page(request):
        demo = request.path == "/miniapp/demo"
        html = (STATIC / "index.html").read_text(encoding="utf-8")
        sdk = '' if demo else '<script defer src="https://telegram.org/js/telegram-web-app.js"></script>'
        return web.Response(text=html.replace("<!-- SDK -->", sdk)
                            .replace("{{entry}}", "app" if demo else "live")
                            .replace("{{mode}}", "Демо · 18+" if demo else "18+"), content_type="text/html")

    async def demo_deck(request):
        return web.json_response(deck())

    async def avatar(request):
        return web.FileResponse(RESOURCES / "default-avatar.png")

    app.router.add_get("/miniapp", page)
    app.router.add_get("/miniapp/", page)
    app.router.add_get("/miniapp/demo", page)
    app.router.add_get("/miniapp/deck", demo_deck)
    app.router.add_get("/miniapp/portrait/{slug}", portrait)
    app.router.add_get("/miniapp/avatar", avatar)
    app.router.add_static("/static/miniapp", STATIC)
    if services is None:
        return app

    profiles, discovery = services.profiles, services.discovery

    def viewer(request):
        account = authenticate(request, bot)
        user = profiles.user_by_telegram(account.id)
        if not user:
            raise web.HTTPForbidden(text="Сначала создай анкету командой /start в боте.")
        if user.banned or user.is_test:
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
                                           "schedule_set", "games", "is_test", "has_photo")}

    async def me(request):
        user, _ = viewer(request)
        own = profiles.public_profile(user)
        return web.json_response({**card(own), "game_ids": own["game_ids"], "active": user.active,
                                  "share_username": user.share_username, "steam_connected": own["steam_connected"],
                                  "catalog": [{"id": g.app_id, "name": g.name} for g in profiles.catalog()]})

    async def edit(request):
        user, account = viewer(request)
        data = await body(request)
        if set(data) != {"name", "bio", "game_ids", "share_username"} or type(data["share_username"]) is not bool:
            raise web.HTTPBadRequest(text="Проверь поля анкеты.")
        share = data.pop("share_username")
        profiles.save_profile(user.telegram_id, None, username=account.username, changes=data, share_username=share)
        return web.json_response({"saved": True})

    async def feed(request):
        user, _ = viewer(request)
        rows = discovery.feed(user.id, limit=1)
        return web.json_response([{**card(r["profile"]), "common_games": r["common_games"]} for r in rows])

    async def react(request):
        user, _ = viewer(request)
        data = await body(request)
        if type(data.get("target")) is not int or not 0 < data["target"] < 2**31 or data.get("kind") not in ("like", "skip"):
            raise web.HTTPBadRequest(text="Некорректная реакция.")
        if data["kind"] == "like" and not user.share_username:
            raise DomainError("Включи обмен @username в своей анкете, затем повтори лайк.")
        result = discovery.react(user.id, data["target"], data["kind"], fresh=True)
        if result["new"]:
            try:
                peer = discovery.match_peer(user.id, result["match_id"])
                await bot.send_message(peer.telegram_id, "💜 У тебя новая взаимность! Открой /matches.", request_timeout=5)
            except (DomainError, TelegramAPIError, TimeoutError):
                pass  # The committed match is still available to both users.
        return web.json_response({"matched": result["matched"]})

    async def undo(request):
        user, _ = viewer(request)
        target = discovery.undo_skip(user.id)
        return web.json_response({"undone": target is not None})

    async def matches(request):
        user, _ = viewer(request)
        return web.json_response([{"id": m["id"], "profile": card(m["profile"])} for m in discovery.matches(user.id)])

    async def contact(request):
        user, _ = viewer(request)
        match_id = int(request.match_info["id"])
        peer = discovery.match_peer(user.id, match_id)
        if not peer.share_username:
            raise DomainError("Собеседник выключил обмен контактами.")
        try:
            username = await current_username(bot, peer)
        except (TelegramAPIError, TimeoutError):
            raise web.HTTPServiceUnavailable(text="Telegram пока не ответил. Попробуй ещё раз.") from None
        if not discovery.match_peer(user.id, match_id).share_username:
            raise DomainError("Контакт недоступен.")
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
            return shown is not None and discovery.feed(
                user.id, preferred_id=target_id, availability_only=True, record_impressions=False)

        if not allowed():
            raise web.HTTPNotFound()
        fixture = profiles.test_photo_file(target_id)
        if fixture:
            return web.FileResponse(fixture)
        file_id = profiles.photo_file(target_id)
        if file_id:
            try:
                result = await bot.download(file_id, timeout=8)
                if result is not None and allowed() and profiles.photo_file(target_id) == file_id:
                    return web.Response(body=result.read(), content_type="image/jpeg")
            except (TelegramAPIError, TimeoutError):
                pass
        return await avatar(request)

    app.add_routes([
        web.get("/miniapp/api/me", me), web.put("/miniapp/api/me", edit),
        web.get("/miniapp/api/feed", feed), web.post("/miniapp/api/react", react),
        web.post("/miniapp/api/undo", undo), web.get("/miniapp/api/matches", matches),
        web.get(r"/miniapp/api/contact/{id:[0-9]{1,10}}", contact),
        web.get(r"/miniapp/api/photo/{id:[0-9]{1,10}}", photo),
    ])
    return app


@asynccontextmanager
async def miniapp_server(settings, services, bot):
    if not settings.miniapp_url:
        yield
        return
    runner = web.AppRunner(create_web_app(services, bot), access_log=None)
    await runner.setup()
    try:
        await web.TCPSite(runner, "0.0.0.0", settings.miniapp_port).start()
        yield
    finally:
        await runner.cleanup()


if __name__ == "__main__":
    web.run_app(create_web_app(), host="127.0.0.1", port=8871, access_log=None)
