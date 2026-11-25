import hashlib
import hmac
import json
import time
from io import BytesIO
from urllib.parse import urlencode

import pytest
from aiohttp.test_utils import TestClient, TestServer
from PIL import Image
from sqlalchemy import func, select

from app.miniapp import create_web_app
from app.miniapp import miniapp_server
from app.persistence.models import Match
from app.services.application import Services
from app.services.fixtures import seed_test_profiles
from tests.telegram.harness import Harness, register


def signed(token, user_id, age=0, *, has_username=True):
    fields = {"auth_date": str(int(time.time()) - age),
              "user": json.dumps({"id": user_id, "first_name": "Игрок",
                                  "username": f"player{user_id}" if has_username else None})}
    payload = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    key = hmac.digest(b"WebAppData", token.encode(), "sha256")
    fields["hash"] = hmac.new(key, payload.encode(), hashlib.sha256).hexdigest()
    return {"Authorization": "tma " + urlencode(fields)}


@pytest.fixture
async def web_bot(service):
    harness = Harness(service)
    async with TestClient(TestServer(create_web_app(service, harness.bot))) as client:
        yield client, harness
    await harness.bot.session.close()


async def test_demo_and_auth(web_bot, users):
    client, h = web_bot
    users(11)
    for path, script in (("/miniapp", "live.js"), ("/miniapp/demo", "app.js")):
        page = await client.get(path)
        assert page.status == 200 and script in await page.text()
    intro = await client.get("/miniapp/intro")
    assert intro.content_type == "image/webp"
    with Image.open(BytesIO(await intro.read())) as animation:
        assert animation.is_animated and animation.n_frames > 1
        assert animation.info["loop"] == 0
        duration = 0
        for frame in range(animation.n_frames):
            animation.seek(frame)
            animation.load()
            duration += animation.info["duration"]
    assert f'data-duration-ms="{duration}"' in await page.text()
    deck = await (await client.get("/miniapp/deck")).json()
    assert len(deck) == 6 and (await client.get(deck[0]["photo"])).status == 200
    for headers in ({}, signed("anotherKey", 11), signed(h.bot.token, 11, 3700), signed(h.bot.token, 11, -60)):
        assert (await client.get("/miniapp/api/me", headers=headers)).status == 401
    new = await (await client.get("/miniapp/api/me", headers=signed(h.bot.token, 12))).json()
    assert not new["registered"] and new["catalog"]
    assert (await client.get("/miniapp/api/feed", headers=signed(h.bot.token, 12))).status == 403
    await h.update(11, "/miniapp")
    assert "/find" in h.sent()[-1].text
    h.settings.miniapp_url = "https://party.example/miniapp"
    await h.update(11, "/miniapp")
    assert h.sent()[-1].reply_markup.inline_keyboard[0][0].web_app.url == h.settings.miniapp_url


async def test_local_debug_keeps_the_api_on_loopback(service, users, unused_tcp_port):
    from aiohttp import ClientSession

    h = Harness(service)
    user = users(73)
    h.settings.miniapp_port = unused_tcp_port
    h.settings.miniapp_debug_telegram_id = 73
    h.settings.miniapp_debug = True
    h.settings.miniapp_url = "https://party.example/miniapp"
    url = h.settings.miniapp_local_url
    async with miniapp_server(h.settings, service, h.bot), ClientSession() as client:
        async with client.get(url) as r:
            page = await r.text()
            assert 'data-local-debug="true"' in page and "telegram-web-app.js" not in page
        async with client.get(url + "/api/me") as r:
            assert r.status == 401
        debug = {"X-PartyUp-Debug": "1"}
        for forwarded in ({"Host": "party.example"}, {"X-Forwarded-For": "192.0.2.1"},
                          {"Origin": "https://party.example"}):
            async with client.get(url + "/api/me", headers=debug | forwarded) as r:
                assert r.status == 403
        async with client.get(url + "/api/bootstrap", headers=debug) as r:
            assert (await r.json())["profile"]["id"] == user.id
        await h.update(73, "/miniapp")
        assert url in h.sent()[-1].text and h.sent()[-1].reply_markup is None
    await h.bot.session.close()


async def test_miniapp_test_feed_is_scoped_and_supports_skip_undo(service, users):
    users(71)
    users(72)
    seed_test_profiles(service)
    normal = Services(service.db, include_test_profiles=False)
    h = Harness(normal)
    async with TestClient(TestServer(create_web_app(normal, h.bot, test_feed_telegram_id=71))) as client:
        path = "/miniapp/api/"
        tester = signed(h.bot.token, 71)
        other = signed(h.bot.token, 72)
        feed = await (await client.get(path + "feed", headers=tester)).json()
        card = feed[0]
        assert card["is_test"]
        queued = feed[1]
        assert (await client.get(path + f"photo/{queued['id']}", headers=tester)).status == 404
        assert (await client.get(path + f"photo/{queued['id']}?prefetch=1", headers=tester)).status == 200
        assert (await client.get(path + f"photo/{queued['id']}?prefetch=1", headers=other)).status == 404
        assert (await client.post(path + "react", json={"target": queued["id"], "kind": "like"},
                                  headers=tester)).status == 409
        regular_feed = await (await client.get(path + "feed", headers=other)).json()
        assert all(not row["is_test"] for row in regular_feed)
        assert (await client.get(path + f"photo/{card['id']}", headers=tester)).status == 200
        assert (await client.get(path + f"photo/{card['id']}", headers=other)).status == 404
        assert (await client.post(path + "react", json={"target": card["id"], "kind": "skip"}, headers=tester)).status == 200
        assert (await (await client.post(path + "undo", headers=tester)).json())["card"]["id"] == card["id"]
        result = await (await client.post(path + "react", json={"target": card["id"], "kind": "like"}, headers=tester)).json()
        assert not result["matched"]
        assert result["feed"][0]["id"] == feed[1]["id"]
    await h.bot.session.close()


@pytest.mark.parametrize("web_first", [True, False])
async def test_cross_surface_match(web_bot, service, web_first):
    client, h = web_bot
    await register(h, telegram=101)
    await register(h, telegram=202)
    a = service.profiles.user_by_telegram(101)
    b = service.profiles.user_by_telegram(202)
    auth = signed(h.bot.token, 101)
    h.session.usernames = {101: "current101", 202: "current202"}
    path = "/miniapp/api/"
    profile = await (await client.get(path + "me", headers=auth)).json()
    assert profile["name"] == a.name and profile["game_ids"] == [105600]
    edit = {"name": "Игрок Mini App", "bio": "Хочу в пати", "share_username": True, "game_ids": [105600]}
    assert (await client.put(path + "me", headers=auth, json=edit)).status == 200
    await h.update(101, "/profile")
    assert edit["name"] in h.photos_sent()[-1].caption

    feed = await (await client.get(path + "feed", headers=auth)).json()
    assert feed[0]["id"] == b.id and "username" not in feed[0]
    await h.update(202, "/find")
    reaction = {"target": b.id, "kind": "like"}
    if not web_first:
        await h.update(202, callback=f"react:like:{a.id}", username="current202")
    result = await client.post(path + "react", json=reaction, headers=auth)
    payload = await result.json()
    assert payload["matched"] == (not web_first)
    assert ("match_id" in payload) == (not web_first)
    if web_first:
        assert await (await client.get(path + "matches", headers=auth)).json() == []
        await h.update(202, callback=f"react:like:{a.id}", username="current202")
    assert any(msg.chat_id == 202 and "@current101" in msg.text for msg in h.sent())
    assert (await client.post(path + "react", json=reaction, headers=auth)).status == 409
    with service.db.session() as session:
        assert session.scalar(select(func.count()).select_from(Match)) == 1
    matches = await (await client.get(path + "matches", headers=auth)).json()
    startup = await (await client.get(path + "bootstrap", headers=auth)).json()
    assert startup["matches"] == matches and startup["profile"]["id"] == a.id and isinstance(startup["feed"], list)
    contact = path + f"contact/{matches[0]['id']}"
    assert await (await client.get(contact, headers=auth)).json() == {"username": "current202"}
    await h.update(202, "/matches", username="current202")
    assert any("@current101" in (msg.caption or "") for msg in h.photos_sent())
    h.session.usernames[202] = None
    assert await (await client.get(contact, headers=auth)).json() == {"username": None}
    h.session.usernames[202] = "renamed202"
    service.profiles.update_telegram_account(202, "old202", share_username=False)
    assert await (await client.get(contact, headers=auth)).json() == {"username": "renamed202"}
    service.discovery.react(b.id, a.id, "block")
    assert (await client.get(contact, headers=auth)).status == 409
    assert await (await client.get(path + "matches", headers=auth)).json() == []


async def test_skip_undo_username_requirement_and_private_photo(web_bot, service, users):
    client, h = web_bot
    a, b = users(301), users(302)
    auth = signed(h.bot.token, 301)
    path = "/miniapp/api/"
    assert (await client.get(path + f"photo/{b.id}", headers=auth)).status == 404
    assert (await client.post(path + "react", json={"target": b.id, "kind": "skip"}, headers=auth)).status == 409
    assert (await client.get(path + "feed", headers=auth)).status == 200
    no_username = signed(h.bot.token, 301, has_username=False)
    assert (await client.post(path + "react", json={"target": b.id, "kind": "like"}, headers=no_username)).status == 409
    assert (await client.post(path + "react", json={"target": b.id, "kind": "skip"}, headers=auth)).status == 200
    photo = path + f"photo/{b.id}"
    assert (await client.get(photo, headers=auth)).status == 404
    assert (await client.get(path + "feed", headers=auth)).headers["X-Can-Undo"] == "1"
    restored = await (await client.post(path + "undo", headers=auth)).json()
    assert restored["undone"] and restored["card"]["id"] == b.id
    assert (await client.get(photo, headers=auth)).content_type == "image/webp"
    service.discovery.react(a.id, b.id, "block")
    assert (await client.get(photo, headers=auth)).status == 404


async def test_registration_consent_duplicate_and_photo(web_bot, service):
    client, h = web_bot
    path = "/miniapp/api/me"
    auth = signed(h.bot.token, 66)
    data = {"name": "Первый вход", "age": 21, "timezone": "Europe/Moscow", "purpose": "friends",
            "bio": "", "game_ids": [1245620]}
    assert (await client.post(path, json=data, headers=auth)).status == 400
    data["consent"] = True
    assert (await client.post(path, json={**data, "age": 17}, headers=auth)).status == 400
    assert service.profiles.user_by_telegram(66) is None
    assert (await client.post(path, json=data, headers=auth)).status == 200
    assert (await client.post(path, json={**data, "name": "Дубликат"}, headers=auth)).status == 409
    user = service.profiles.user_by_telegram(66)
    assert user.name == data["name"] and user.use_default_avatar and user.share_username
    assert (await client.put(path + "/photo", data=b"bytes", headers=auth)).status == 400
    assert (await client.put(path + "/photo", data=b"photo", headers={**auth, "Content-Type": "image/jpeg"})).status == 200
    assert service.profiles.photo_file(user.id) and h.photos_sent()[-1].chat_id == 66


async def test_configured_http_server_and_uploaded_photo(service, users, unused_tcp_port, monkeypatch):
    from aiohttp import ClientSession

    h = Harness(service)
    users(801)
    peer = users(802)
    service.profiles.set_photo(peer.id, "uploaded-photo")
    source = BytesIO()
    Image.new("RGB", (1280, 900), "purple").save(source, "JPEG")
    downloads = []

    async def download(bot, file_id, *, timeout):
        downloads.append(file_id)
        return BytesIO(source.getvalue())

    monkeypatch.setattr(type(h.bot), "download", download)
    h.settings.miniapp_url = "https://party.example/miniapp"
    h.settings.miniapp_port = unused_tcp_port
    url = f"http://127.0.0.1:{unused_tcp_port}/miniapp/api"
    auth = signed(h.bot.token, 801)
    async with miniapp_server(h.settings, service, h.bot), ClientSession(headers=auth) as client:
        async with client.get(url + "/feed") as response:
            assert (await response.json())[0]["has_photo"]
        for _ in range(2):
            async with client.get(f"{url}/photo/{peer.id}") as response:
                assert response.content_type == "image/webp"
                with Image.open(BytesIO(await response.read())) as image:
                    assert image.width == 540 and image.height < 540
                assert response.headers["Cache-Control"] == "no-store"
        assert downloads == ["uploaded-photo"]
        service.discovery.react(service.profiles.user_by_telegram(801).id, peer.id, "block")
        async with client.get(f"{url}/photo/{peer.id}") as response:
            assert response.status == 404
    await h.bot.session.close()
