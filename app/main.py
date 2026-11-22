import asyncio
import contextlib
import logging
from contextlib import asynccontextmanager
from html import escape
from urllib.parse import urlencode

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import delete

from .config import ROOT, Settings
from .db import Database
from .models import Block, Event, Match, Reaction, Report
from .recommender import AffinityModel
from .schemas import ProfileInput, ReactionInput
from .seed import seed_demo
from .dota import OpenDotaClient
from .service import DomainError, Service
from .steam import SteamClient
from .steam_public import PublicSteamParser
from .test_profiles import seed_test_profiles

log = logging.getLogger(__name__)


def create_app(settings=None, database=None, start_bot=True):
    settings = settings or Settings()
    db = database or Database(settings.db_url)
    db.initialize()
    service = Service(db, AffinityModel(ROOT / "data/affinity.json"))

    @asynccontextmanager
    async def lifespan(app):
        # Library HTTP info logging can expose API-key query parameters.
        logging.getLogger("httpx").setLevel(logging.WARNING)
        logging.getLogger("httpcore").setLevel(logging.WARNING)
        client = httpx.AsyncClient(timeout=20, follow_redirects=False)
        app.state.steam = SteamClient(settings.steam_api_key, client, OpenDotaClient(client))
        app.state.public_steam = PublicSteamParser(client)
        app.state.bot = None
        app.state.polling = None
        if settings.demo:
            seed_demo(service)
        else:
            seed_test_profiles(service)
        try:
            if not settings.demo and start_bot:
                from .bot import build_bot, run_polling
                bot, dispatcher = build_bot(service, settings, app.state.steam, app.state.public_steam)
                app.state.bot = bot
                try:
                    await bot.get_me()
                except Exception:
                    raise RuntimeError("Не удалось подключить Telegram. Проверьте BOT_TOKEN и сеть") from None
                app.state.polling = asyncio.create_task(run_polling(bot, dispatcher, settings, service))
            yield
        finally:
            if app.state.polling:
                app.state.polling.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await app.state.polling
            if app.state.bot:
                await app.state.bot.session.close()
            await client.aclose()
            db.engine.dispose()

    app = FastAPI(title="PartyUp", version="0.1.0", lifespan=lifespan,
                  docs_url="/docs" if settings.demo else None, redoc_url=None,
                  openapi_url="/openapi.json" if settings.demo else None)
    app.state.service, app.state.settings = service, settings
    app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")

    @app.middleware("http")
    async def security_headers(request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Cache-Control"] = "no-store"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
            "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        )
        return response

    @app.exception_handler(DomainError)
    async def domain_error(request, error):
        return JSONResponse({"detail": str(error)}, status_code=400)

    def demo_viewer():
        if not settings.demo:
            raise HTTPException(404, "Not found")
        viewer = service.user_by_telegram(-1)
        if viewer is None or not viewer.is_demo:
            raise HTTPException(503, "Демо запускается")
        return viewer

    @app.get("/", include_in_schema=False)
    def index():
        if settings.demo:
            return FileResponse(ROOT / "static/index.html")
        return HTMLResponse("<html lang='ru'><meta charset='utf-8'><title>PartyUp</title>"
                            "<h1>PartyUp</h1><p>Сервис знакомств для геймеров работает через Telegram.</p>"
                            "<p>Управление данными: /privacy и /delete в боте.</p></html>")

    @app.get("/health")
    def health():
        polling = getattr(app.state, "polling", None)
        running = polling is not None and not polling.done()
        return {"status": "ok" if settings.demo or running or not start_bot else "degraded",
                "mode": "demo" if settings.demo else "telegram", "bot_running": running,
                "model": service.model.version}

    @app.get("/model", response_class=HTMLResponse)
    def model_info():
        report = service.model.data.get("metrics", {})
        return (
            "<html lang='ru'><meta charset='utf-8'><title>PartyUp · модель</title><h1>Модель игровых интересов</h1>"
            "<p>В подборе 60% веса у главной общей игры, 30% у остальных общих игр и 10% у жанров. "
            "При равных баллах анкеты сортируются по пересечению расписания.</p>"
            "<p>Содержит информацию из Steam Video Games (Tamber, Kaggle), доступной по ODbL 1.0 / DbCL 1.0.</p>"
            '<p><a href="https://www.kaggle.com/datasets/tamber/steam-video-games">Исходный датасет</a> · '
            '<a href="/model/affinity.json">Производная база связей игр (ODbL)</a> · '
            '<a href="/model/report.json">Отчёт</a> · <a href="/model/license">ODbL</a> · '
            '<a href="/model/contents-license">DbCL</a></p>'
            f"<p>Отдельный эксперимент с item-item моделью. Hit rate@10: {report.get('hit_rate_at_10', 'нет данных')}; популярность: "
            f"{report.get('popularity_hit_rate_at_10', 'нет данных')}.</p>"
            "<p>Данные 2017 года. Метрика оценивает восстановление интереса к игре, а не совместимость людей. "
            "Для новых игр применяется пересечение выбранных игр и редакционный каталог жанров. "
            "Идентификаторы аккаунтов из датасета не публикуются и не используются как анкеты.</p></html>"
        )

    @app.get("/model/affinity.json")
    def model_file():
        return FileResponse(ROOT / "data/affinity.json", media_type="application/json")

    @app.get("/model/report.json")
    def model_report():
        return FileResponse(ROOT / "data/model_report.json", media_type="application/json")

    @app.get("/model/license")
    def model_license():
        return FileResponse(ROOT / "licenses/ODbL-1.0.txt", media_type="text/plain; charset=utf-8")

    @app.get("/model/contents-license")
    def contents_license():
        return FileResponse(ROOT / "licenses/DbCL-1.0.txt", media_type="text/plain; charset=utf-8")

    @app.get("/api/demo/profile")
    def demo_profile():
        return service.public_profile(demo_viewer())

    @app.put("/api/demo/profile")
    def update_demo(profile: ProfileInput):
        viewer = demo_viewer()
        user_id = service.save_profile(viewer.telegram_id, profile, demo=True)
        return service.public_profile(service.user(user_id))

    @app.get("/api/demo/catalog")
    def demo_catalog():
        demo_viewer()
        return [{"app_id": game.app_id, "name": game.name, "genres": game.genres} for game in db.catalog()]

    @app.get("/api/demo/feed")
    def demo_feed():
        return service.feed(demo_viewer().id)

    @app.post("/api/demo/reaction")
    def demo_reaction(reaction: ReactionInput):
        return service.react(demo_viewer().id, reaction.target_id, reaction.kind, reaction.reason)

    @app.get("/api/demo/matches")
    def demo_matches():
        return service.matches(demo_viewer().id)

    @app.post("/api/demo/reset")
    def demo_reset():
        viewer = demo_viewer()
        with service.lock, db.session.begin() as session:
            for model, condition in [
                (Match, (Match.low_id == viewer.id) | (Match.high_id == viewer.id)),
                (Reaction, Reaction.actor_id == viewer.id), (Block, Block.actor_id == viewer.id),
                (Report, Report.actor_id == viewer.id), (Event, Event.actor_id == viewer.id),
            ]:
                session.execute(delete(model).where(condition))
        return {"ok": True}

    @app.get("/steam/login")
    def steam_login(ticket: str):
        if settings.demo or settings.steam_mode != "openid":
            raise HTTPException(404)
        browser = service.begin_login(ticket)
        return_to = settings.public_url + "/steam/callback?" + urlencode({"ticket": ticket})
        response = RedirectResponse(SteamClient.login_url(return_to, settings.public_url + "/"), status_code=303)
        response.set_cookie("steam_session", browser, httponly=True, secure=True,
                            samesite="lax", max_age=600, path="/steam")
        return response

    @app.get("/steam/callback", response_class=HTMLResponse)
    async def steam_callback(request: Request, ticket: str):
        if settings.demo or settings.steam_mode != "openid":
            raise HTTPException(404)
        browser = request.cookies.get("steam_session", "")
        service.verify_ticket(ticket, browser)
        expected = settings.public_url + "/steam/callback?" + urlencode({"ticket": ticket})
        steam_id = await app.state.steam.verify(dict(request.query_params), expected)
        user_id = service.link_steam(ticket, browser, steam_id)
        user = service.user(user_id)
        text = "Аккаунт Steam подтверждён."
        try:
            profile = await app.state.steam.game_profile(steam_id)
            service.save_public_steam(user_id, profile, expected_steam_id=steam_id)
            count = len(profile.games)
            source = "недавних игр" if profile.coverage == "recent" else "игр"
            text += f" Импортировано {source}: {count}."
            if profile.dota_match_minutes:
                text += " Для Dota 2 учтена оценка наигрыша по данным OpenDota."
        except DomainError as error:
            text += " " + str(error)
        if app.state.bot:
            try:
                await app.state.bot.send_message(user.telegram_id, escape(text))
            except Exception as error:
                log.warning("Steam notification failed: %s", type(error).__name__)
        response = HTMLResponse("<html lang='ru'><meta charset='utf-8'><title>PartyUp · Steam</title>"
                                f"<h1>Steam подключён</h1><p>{escape(text)}</p><p>Можно вернуться в Telegram.</p></html>")
        response.delete_cookie("steam_session", path="/steam", secure=True, httponly=True, samesite="lax")
        return response

    return app


def main():
    import uvicorn
    settings = Settings()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    # Access logs contain the one-use OpenID query string; do not record them.
    uvicorn.run(create_app(settings), host=settings.host, port=settings.port, access_log=False)


if __name__ == "__main__":
    main()
