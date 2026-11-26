"""Measure PartyUp transport, Telegram Bot API, and an isolated application workload."""
import argparse
import asyncio
import csv
import json
import logging
import math
import os
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from secrets import token_urlsafe
from statistics import median
from time import perf_counter
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
from aiogram import Bot
from aiohttp import web
from sqlalchemy import insert
from sqlalchemy.schema import CreateSchema, DropSchema

from app.config import RESOURCES, Settings
from app.miniapp import create_web_app
from app.persistence.database import Database
from app.persistence.models import User, UserGame
from app.services.application import Services

PUBLIC = {
    "HTML": ("/miniapp", "text/html", b"<title>PartyUp"),
    "JS": ("/static/miniapp/live.js", "javascript", b"prepareTabs"),
    "Intro": ("/miniapp/intro?loop=1", "image/webp", b"RIFF"),
    "Avatar": ("/miniapp/avatar", "image/webp", b"RIFF"),
}
LAB = {
    "Bootstrap": ("/miniapp/api/bootstrap", "application/json", b'"registered": true'),
    "Deck": ("/miniapp/api/feed", "application/json", b'"common_games"'),
    "Portrait": ("/miniapp/api/photo/1", "image/webp", b"RIFF"),
}
TEST_USER = 910001


class LabBot(Bot):
    async def download(self, file, destination=None, timeout=30, chunk_size=65536, seek=True):
        return BytesIO((RESOURCES / "test_profiles/lera.jpg").read_bytes())


@dataclass
class Sample:
    phase: str
    origin: str
    scenario: str
    round: int
    ok: bool
    status: str
    elapsed_ms: float
    headers_ms: float | None = None
    bytes: int | None = None


def origin(value):
    name, sep, url = value.partition("=")
    parsed = urlsplit(url)
    if not sep or not name or parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise argparse.ArgumentTypeError("Expected NAME=http(s)://HOST[:PORT]")
    if (parsed.path not in ("", "/", "/miniapp", "/miniapp/") or parsed.query or parsed.fragment
            or parsed.username is not None or parsed.password is not None):
        raise argparse.ArgumentTypeError("Expected an origin or its /miniapp URL")
    return name, f"{parsed.scheme}://{parsed.netloc}"


async def request(client, samples, phase, name, url, scenario, spec, number, auth=None):
    path, media, signature = spec
    start = perf_counter()
    headers = None
    try:
        async with client.stream("GET", url + path, headers=auth) as response:
            headers = 1000 * (perf_counter() - start)
            body = await response.aread()
            ok = response.status_code == 200 and media in response.headers.get("content-type", "") and signature in body
            status = str(response.status_code) if ok or response.status_code != 200 else "wrong_payload"
            size = len(body)
    except httpx.HTTPError as error:
        ok, status, size = False, type(error).__name__, None
    samples.append(Sample(phase, name, scenario, number, ok, status, 1000 * (perf_counter() - start), headers, size))


async def http_rounds(samples, phase, origins, scenarios, rounds, *, cold=0, auth=None):
    print(f"{phase}: {rounds} rounds across {len(origins)} origins", flush=True)
    async with httpx.AsyncClient(timeout=5, limits=httpx.Limits(max_keepalive_connections=len(origins))) as client:
        for number in range(1, rounds + 1):
            ordered = origins if number % 2 else origins[::-1]
            for scenario, spec in scenarios.items():
                for name, url in ordered:
                    await request(client, samples, phase, name, url, scenario, spec, number, auth)
                await asyncio.sleep(0.2)
            if number % 6 == 0:
                print(f"{phase}: {number}/{rounds} rounds", flush=True)
        for number in range(1, cold + 1):
            for name, url in (origins if number % 2 else origins[::-1]):
                async with httpx.AsyncClient(timeout=5) as fresh:
                    await request(fresh, samples, phase + "-cold", name, url, "HTML", PUBLIC["HTML"], number, auth)
            await asyncio.sleep(0.2)


async def telegram_api(samples, token, rounds):
    print(f"Telegram Bot API: {rounds} getMe requests", flush=True)
    async with Bot(token).context() as bot:
        for number in range(1, rounds + 1):
            start = perf_counter()
            try:
                await bot.get_me(request_timeout=5)
                ok, status = True, "ok"
            except Exception as error:
                ok, status = False, type(error).__name__
            samples.append(Sample("bot-network", "PC to Telegram", "getMe", number, ok, status,
                                  1000 * (perf_counter() - start)))
            await asyncio.sleep(0.4)


@asynccontextmanager
async def isolated_db(url):
    root = Database(url).engine
    schema = f"partyup_bench_{uuid4().hex}"
    with root.begin() as connection:
        connection.execute(CreateSchema(schema))
    db = Database(root.url.update_query_dict({"options": f"-csearch_path={schema}"}))
    try:
        db.initialize()
        yield db
    finally:
        db.engine.dispose()
        with root.begin() as connection:
            connection.execute(DropSchema(schema, cascade=True))
        root.dispose()


def seed(db, count):
    with db.session.begin() as session:
        ids = session.scalars(insert(User).returning(User.id), [{
            "telegram_id": TEST_USER + i, "name": f"Bench {i}", "age": 25, "username": f"bench{i}",
            "bio": "Синтетическая анкета для измерения времени ответа.", "use_default_avatar": True,
            "photo_file_id": "bench-photo" if i == 0 else None,
        } for i in range(count)]).all()
        session.execute(insert(UserGame), [
            {"user_id": user_id, "app_id": game, "selected": True}
            for user_id in ids for game in (105600, 548430)
        ])


async def dispatcher_rounds(samples, harness, rounds, count):
    for number in range(1, rounds + 1):
        for scenario in ("/start new", "/profile", "/find"):
            start = perf_counter()
            try:
                account = TEST_USER + count + number if scenario == "/start new" else TEST_USER
                harness.session.calls.clear()
                await harness.update(account, scenario.split()[0], username="bench0")
                ok = bool(harness.session.calls)
                status = "ok" if ok else "no_response"
            except Exception as error:
                ok, status = False, type(error).__name__
            samples.append(Sample(f"bot-dispatch-{count}", "Isolated PostgreSQL", scenario, number,
                                  ok, status, 1000 * (perf_counter() - start)))


async def browser_rounds(samples, origins, auth, rounds):
    from playwright.async_api import async_playwright

    sdk = 'window.Telegram={WebApp:{initData:' + json.dumps(auth["Authorization"][4:]) + (
        ',ready(){},expand(){},isVersionAtLeast(){return false;}}};'
    )
    print(f"Cold browser: {rounds} launches across {len(origins)} origins", flush=True)
    async with async_playwright() as p:
        browser = await p.chromium.launch(executable_path=os.environ.get("PARTYUP_TEST_CHROME") or None)
        try:
            for number in range(1, rounds + 1):
                for name, url in (origins if number % 2 else origins[::-1]):
                    page = await browser.new_page(viewport={"width": 390, "height": 844}, has_touch=True)
                    start = perf_counter()
                    try:
                        await page.route("https://telegram.org/js/telegram-web-app.js", lambda route: route.fulfill(
                            content_type="application/javascript", body=sdk))
                        start = perf_counter()
                        response = await page.goto(url + "/miniapp", wait_until="domcontentloaded", timeout=12000)
                        if response is not None and response.status == 200:
                            await page.locator("#player").wait_for(state="visible", timeout=15000)
                            ok, status = True, "200"
                        else:
                            ok, status = False, str(response.status) if response else "no_response"
                    except Exception as error:
                        ok, status = False, type(error).__name__
                    finally:
                        elapsed = 1000 * (perf_counter() - start)
                        await page.close()
                    samples.append(Sample("browser-cold", name, "First card", number, ok, status,
                                          elapsed))
        finally:
            await browser.close()


async def lab(samples, args, count, *, through_tunnel):
    from tests.telegram.harness import Harness
    from tests.test_miniapp import signed

    async with isolated_db(os.environ["PARTYUP_BENCH_DATABASE_URL"]) as db:
        seed(db, count)
        service = Services(db)
        harness = Harness(service)
        await dispatcher_rounds(samples, harness, args.rounds, count)
        bot = LabBot("123456789:" + token_urlsafe(32), session=harness.session)
        auth = signed(bot.token, TEST_USER)
        runner = web.AppRunner(create_web_app(service, bot), access_log=None)
        await runner.setup()
        try:
            await web.TCPSite(runner, "127.0.0.1", args.lab_port).start()
            origins = [("Lab local", f"http://127.0.0.1:{args.lab_port}")]
            if through_tunnel and args.lab_origin:
                origins.append(args.lab_origin)
            await http_rounds(samples, f"api-{count}", origins, LAB, args.rounds, auth=auth)
            if through_tunnel and args.browser:
                await browser_rounds(samples, origins, auth, args.browser)
        finally:
            await runner.cleanup()
            await harness.bot.session.close()


def percentile_95(values):
    ordered = sorted(values)
    return ordered[math.ceil(0.95 * len(ordered)) - 1]


def report(samples, dest, started):
    dest.mkdir(parents=True, exist_ok=True)
    with (dest / "samples.csv").open("w", newline="", encoding="utf-8-sig") as file:
        writer = csv.DictWriter(file, fieldnames=list(Sample.__dataclass_fields__))
        writer.writeheader()
        writer.writerows(asdict(x) for x in samples)
    groups = {}
    for sample in samples:
        groups.setdefault((sample.phase, sample.origin, sample.scenario), []).append(sample)
    result = []
    for (phase, origin_name, scenario), entries in groups.items():
        good = [e for e in entries if e.ok]
        times = [e.elapsed_ms for e in good]
        stamps = [e.headers_ms for e in good if e.headers_ms is not None]
        sizes = [e.bytes for e in good if e.bytes is not None]
        result.append(dict(phase=phase, origin=origin_name, scenario=scenario, attempts=len(entries), successes=len(good),
                           failures={status: sum(e.status == status and not e.ok for e in entries)
                                     for status in sorted({e.status for e in entries if not e.ok})},
                           p50_ms=round(median(times), 2) if times else None,
                           p95_ms=round(percentile_95(times), 2) if times else None,
                           headers_p50_ms=round(median(stamps), 2) if stamps else None,
                           bytes_p50=median(sizes) if sizes else None))
    (dest / "summary.json").write_text(json.dumps({
        "started_utc": started.isoformat(), "finished_utc": datetime.now(timezone.utc).isoformat(), "summary": result,
    }, ensure_ascii=False, indent=2), encoding="utf-8")


async def main(args):
    samples = []
    started = datetime.now(timezone.utc)
    try:
        origins = list(args.origin)
        if args.live:
            settings = Settings()
            if settings.miniapp_debug or not settings.miniapp_url:
                raise SystemExit("Live comparison requires an enabled public Mini App.")
            origins[:0] = [origin("PC local=" + settings.miniapp_local_url),
                           origin("Current tunnel=" + settings.miniapp_url)]
        if origins:
            await http_rounds(samples, "public-warm", origins, PUBLIC, args.rounds, cold=args.cold)
        if args.live:
            await telegram_api(samples, settings.bot_token, args.rounds)
        for index, count in enumerate(args.lab_profiles):
            await lab(samples, args, count, through_tunnel=index == 0)
    finally:
        report(samples, args.output, started)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="Use the configured PartyUp deployment and read-only Telegram getMe.")
    parser.add_argument("--origin", type=origin, action="append", default=[], help="Additional public endpoint, NAME=URL.")
    parser.add_argument("--rounds", type=int, default=24)
    parser.add_argument("--cold", type=int, default=10)
    parser.add_argument("--lab-profiles", type=int, nargs="*", default=[], help="Accounts in each isolated PostgreSQL schema.")
    parser.add_argument("--lab-origin", type=origin, help="HTTPS tunnel to the isolated benchmark server.")
    parser.add_argument("--lab-port", type=int, default=8874)
    parser.add_argument("--browser", type=int, default=0, help="Cold Chrome launches per isolated endpoint.")
    parser.add_argument("--output", type=Path, required=True)
    options = parser.parse_args()
    if not 1 <= options.rounds <= 100 or not 0 <= options.cold <= 50 or not 0 <= options.browser <= 30:
        parser.error("Invalid rounds, cold, or browser count.")
    if options.lab_profiles and (not os.environ.get("PARTYUP_BENCH_DATABASE_URL")
                                 or any(not 3 <= count <= 2000 for count in options.lab_profiles)):
        parser.error("Set PARTYUP_BENCH_DATABASE_URL to a test database and use 3–2000 lab profiles.")
    if not (options.live or options.origin or options.lab_profiles):
        parser.error("Select --live, --origin, or --lab-profiles.")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    asyncio.run(main(options))
