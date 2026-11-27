"""Bounded, unauthenticated parsing of public Steam Community pages.

No cookies, private data, login automation, CAPTCHA or access-control bypass.
Steam can require login even for public libraries; callers receive explicit
coverage information and can fall back to manual interests.
"""

import asyncio
import json
import math
import re
import time
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit
from xml.etree import ElementTree

import httpx

from app.domain.errors import DomainError
from app.domain.steam import PublicProfile

from .http import api_issue, api_skipped, stream_api

MAX_BYTES = 4_000_000
ID_PATTERN = re.compile(r"7656119\d{10}")


def normalize_profile_url(value: str) -> str:
    value = value.strip()
    if value.startswith("steamcommunity.com/"):
        value = "https://" + value
    try:
        parsed = urlsplit(value)
        valid_host = (
            parsed.scheme in {"http", "https"}
            and parsed.hostname == "steamcommunity.com"
            and not parsed.username
            and not parsed.password
            and parsed.port in {None, 80, 443}
        )
    except ValueError:
        valid_host = False
    if not valid_host or not re.fullmatch(
        r"/(?:id/[A-Za-z0-9_-]{2,64}|profiles/7656119\d{10})/?", parsed.path
    ):
        raise DomainError(
            "Пришли ссылку вида https://steamcommunity.com/id/ник или https://steamcommunity.com/profiles/SteamID"
        )
    return "https://steamcommunity.com" + parsed.path.rstrip("/")


def xml_root(text: str):
    if len(text.encode("utf-8")) > MAX_BYTES or re.search(r"<!\s*(?:DOCTYPE|ENTITY)", text, re.I):
        raise DomainError("Не удалось безопасно прочитать ответ Steam")
    try:
        return ElementTree.fromstring(text)
    except ElementTree.ParseError:
        raise DomainError(
            "Steam не вернул открытые данные профиля. Попробуй позже или выбери игры вручную"
        ) from None


def app_id_from_url(value):
    match = re.search(r"/(?:app|games)/(\d+)(?:/|$)", value or "")
    return int(match.group(1)) if match else None


def minutes(value):
    try:
        number = float(str(value).replace(",", ""))
        return min(round(max(number, 0) * 60), 60 * 24 * 366 * 30) if math.isfinite(number) else 0
    except (TypeError, ValueError):
        return 0


def recent_xml_games(root):
    result = []
    for game in root.findall("./mostPlayedGames/mostPlayedGame"):
        app_id = app_id_from_url(game.findtext("gameLink"))
        if app_id:
            result.append(
                {
                    "appid": app_id,
                    "name": game.findtext("gameName") or f"Steam {app_id}",
                    "playtime_forever": 0,
                    "playtime_2weeks": minutes(game.findtext("hoursPlayed")),
                }
            )
    return result


def parse_games_xml(text):
    root = xml_root(text)
    if root.tag != "gamesList" or root.find("games") is None or root.find("error") is not None:
        return None
    result = []
    for game in root.findall("./games/game"):
        app_id = game.findtext("appID", "")
        if app_id.isdigit() and int(app_id) > 0:
            result.append(
                {
                    "appid": int(app_id),
                    "name": game.findtext("name") or f"Steam {app_id}",
                    "playtime_forever": minutes(game.findtext("hoursOnRecord")),
                    "playtime_2weeks": minutes(game.findtext("hoursLast2Weeks")),
                }
            )
    return result


class Scripts(HTMLParser):
    def __init__(self):
        super().__init__()
        self.inside = False
        self.items = []

    def handle_starttag(self, tag, attrs):
        if tag == "script":
            self.inside = True

    def handle_data(self, data):
        if self.inside:
            self.items.append(data)

    def handle_endtag(self, tag):
        if tag == "script":
            self.inside = False


def parse_games_html(text):
    """Legacy g_rgGames/rgGames JSON only; never execute scripts from a profile."""
    if len(text.encode("utf-8")) > MAX_BYTES:
        raise DomainError("Ответ Steam слишком большой")
    parser = Scripts()
    parser.feed(text)
    for script in parser.items:
        match = re.search(r"\b(?:g_rgGames|rgGames)\s*=\s*", script)
        if not match:
            continue
        try:
            rows, _ = json.JSONDecoder().raw_decode(script[match.end() :])
        except (ValueError, TypeError):
            continue
        if not isinstance(rows, list):
            continue
        result = []
        for row in rows:
            if not isinstance(row, dict) or not str(row.get("appid", "")).isdigit():
                continue
            result.append(
                {
                    "appid": int(row["appid"]),
                    "name": str(row.get("name", ""))[:160],
                    "playtime_forever": minutes(row.get("hours_forever", 0)),
                    "playtime_2weeks": minutes(row.get("hours", 0)),
                }
            )
        return result
    return None


class PublicSteamParser:
    def __init__(self, client: httpx.AsyncClient, interval=1.0):
        self.client, self.interval = client, interval
        self.lock = asyncio.Lock()
        self.last_request = 0.0

    async def get(self, url, *, operation):
        # Follow a bounded number of same-origin redirects; never contact a user-supplied host.
        for _ in range(4):
            try:
                parsed = urlsplit(url)
                allowed = (
                    parsed.scheme == "https" and parsed.hostname == "steamcommunity.com"
                    and parsed.port in {None, 443}
                )
            except ValueError:
                allowed = False
            if not allowed:
                api_issue("steam_community", operation, "invalid_redirect")
                raise DomainError("Steam перенаправил запрос за пределы своего сайта")
            if parsed.path.startswith("/login"):
                api_skipped("steam_community", operation, "login_required")
                return None
            async with self.lock:
                await asyncio.sleep(max(0.0, self.interval - (time.monotonic() - self.last_request)))
                self.last_request = time.monotonic()
                try:
                    async with stream_api(
                        self.client, "steam_community", operation, "GET",
                        url,
                        headers={
                            "User-Agent": "PartyUp/0.1 public-profile reader",
                            "Accept-Language": "en-US,en;q=0.9",
                        },
                        follow_redirects=False,
                    ) as response:
                        if response.status_code in {301, 302, 303, 307, 308}:
                            try:
                                url = urljoin(url, response.headers.get("location", ""))
                            except ValueError:
                                api_issue("steam_community", operation, "invalid_redirect")
                                raise DomainError("Steam вернул некорректное перенаправление") from None
                            continue
                        if response.status_code in {401, 403, 429}:
                            return None
                        response.raise_for_status()
                        chunks, size = [], 0
                        async for chunk in response.aiter_bytes():
                            size += len(chunk)
                            if size > MAX_BYTES:
                                api_issue("steam_community", operation, "response_too_large")
                                raise DomainError("Ответ Steam слишком большой")
                            chunks.append(chunk)
                        return b"".join(chunks).decode("utf-8", "replace")
                except (httpx.HTTPError, httpx.InvalidURL):
                    raise DomainError(
                        "Steam временно недоступен. Сохранённые интересы остаются доступны"
                    ) from None
        api_issue("steam_community", operation, "too_many_redirects")
        raise DomainError("Слишком много перенаправлений Steam")

    async def fetch(self, value: str):
        url = normalize_profile_url(value)
        text = await self.get(url + "/?xml=1", operation="profile_xml")
        if text is None:
            raise DomainError(
                "Steam сейчас не отдаёт профиль без входа. Можно сохранить интересы вручную и попробовать позже"
            )
        try:
            root = xml_root(text)
        except DomainError:
            api_issue("steam_community", "profile_xml", "invalid_xml")
            raise
        steam_id = root.findtext("steamID64", "")
        if root.tag != "profile" or not ID_PATTERN.fullmatch(steam_id):
            raise DomainError("Профиль Steam не найден. Проверь ссылку")
        result = PublicProfile(steam_id, "https://steamcommunity.com/profiles/" + steam_id)
        if root.findtext("privacyState") != "public":
            result.warning = "Ссылка сохранена. Профиль или игры скрыты; выбери актуальные игры вручную"
            return result
        result.games = recent_xml_games(root)
        result.coverage = "recent" if result.games else "profile_only"
        # One attempt at the library; login redirects and denials are respected.
        try:
            library = await self.get(result.url + "/games/?tab=all&xml=1", operation="library")
            games = None
            if library and library.lstrip().startswith("<?xml"):
                games = parse_games_xml(library)
            elif library:
                games = parse_games_html(library)
            if games is not None:
                result.games, result.coverage = games, "library"
        except DomainError:
            pass
        if result.coverage == "recent":
            result.warning = "Удалось прочитать только недавние игры. Полная библиотека недоступна без входа; актуальные игры можно выбрать вручную"
        elif result.coverage == "profile_only":
            result.warning = (
                "Ссылка сохранена. Steam не отдал список игр без входа; выбери актуальные игры вручную"
            )
        return result
