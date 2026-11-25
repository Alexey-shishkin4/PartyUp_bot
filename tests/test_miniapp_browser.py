import json
import os
from asyncio import Event

from aiohttp.test_utils import TestServer
from playwright.async_api import async_playwright, expect

from app.miniapp import create_web_app
from tests.telegram.harness import Harness
from tests.test_miniapp import signed


async def test_mobile_and_desktop_miniapp(service, users):
    a, b = users(501), users(502)
    harness = Harness(service)
    service.profiles.set_photo(a.id, "own-photo")
    harness.session.usernames = {501: "current501", 502: "current502"}
    service.profiles.update_telegram_account(502, "old502", share_username=True)
    await harness.update(502, "/find")
    await harness.update(502, callback=f"react:like:{a.id}", username="current502")
    auth = signed(harness.bot.token, 501)["Authorization"].removeprefix("tma ")
    sdk = "window.Telegram = {WebApp: {initData: " + json.dumps(auth) + """,
      ready() {}, expand() {}, isVersionAtLeast() {return true;},
      BackButton: {onClick() {}, hide() {}, show() {}}
    }};"""
    async with TestServer(create_web_app(service, harness.bot)) as server, async_playwright() as p:
        browser = await p.chromium.launch(executable_path=os.environ.get("PARTYUP_TEST_CHROME") or None)
        try:
            page = await browser.new_page(viewport={"width": 390, "height": 844})
            errors = []
            requests = []
            finish_photo = Event()
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.on("request", lambda request: requests.append(request.url))

            async def slow_photo(route):
                await finish_photo.wait()
                await route.fulfill(status=503, body="Unavailable")

            await page.route(f"**/miniapp/api/photo/{a.id}", slow_photo)
            await page.route("https://telegram.org/js/telegram-web-app.js",
                             lambda route: route.fulfill(body=sdk, content_type="application/javascript"))
            await page.goto(str(server.make_url("/miniapp")))
            await expect(page.locator("#name")).to_contain_text(b.name)
            assert [url.split("/api/")[1] for url in requests if "/api/" in url] == ["bootstrap"]
            assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            await page.locator("#like").click()
            await expect(page.locator("#status")).to_contain_text("обмен")
            await page.locator("[data-tab=profile]").click()
            await expect(page.locator("#own-name")).to_contain_text(a.name)
            await expect(page.locator("#own-card .portrait")).to_have_attribute("aria-busy", "true")
            await expect(page.locator("#edit-profile")).to_be_enabled()
            finish_photo.set()
            await expect(page.locator("#own-photo")).to_have_attribute("src", "/miniapp/avatar")
            await page.locator("#edit-profile").click()
            await page.locator("#nickname").fill("Мобильный игрок")
            await page.locator("#share").check()
            await page.locator("#editor button").click()
            await expect(page.locator("#status")).to_have_text("Анкета сохранена.")
            await expect(page.locator("#own-name")).to_contain_text("Мобильный игрок")
            await page.locator("#go-back").click()
            await page.locator("#pass").click()
            await expect(page.locator("#empty")).to_be_visible()
            await page.reload()
            await page.locator("#undo-button").click()
            await expect(page.locator("#player")).to_be_visible()
            await page.locator("#like").click()
            await expect(page.locator("#mutual")).to_be_visible()
            await page.locator("#show-matches").click()
            await page.get_by_role("button", name="Показать контакт").click()
            await expect(page.get_by_role("link", name="@current502")).to_have_attribute("href", "https://t.me/current502")
            await page.reload()
            await page.locator("[data-tab=profile]").click()
            await expect(page.locator("#own-name")).to_contain_text("Мобильный игрок")
            await page.locator("#edit-profile").click()
            await page.locator("#go-back").click()
            await expect(page.locator("#own-card")).to_be_visible()
            first = signed(harness.bot.token, 503)["Authorization"].removeprefix("tma ")
            await page.unroute("https://telegram.org/js/telegram-web-app.js")
            await page.route("https://telegram.org/js/telegram-web-app.js", lambda route: route.fulfill(
                body=sdk.replace(json.dumps(auth), json.dumps(first)), content_type="application/javascript"))
            await page.reload()
            await expect(page.locator("#profile-title")).to_have_text("Создать анкету")
            await page.locator("#nickname").fill("Новый игрок")
            await page.locator("#age").fill("24")
            await page.locator("#zone").fill("Asia/Novosibirsk")
            await page.locator("#game-search").fill("ELDEN")
            await page.locator("#choices label:visible").first.click()
            await page.locator("#consent").check()
            await page.locator("#save-profile").click()
            await expect(page.locator("#own-games")).to_contain_text("ELDEN RING")
            assert service.profiles.user_by_telegram(503).age == 24
            await page.set_viewport_size({"width": 1280, "height": 860})
            await page.goto(str(server.make_url("/miniapp/demo")))
            await expect(page.locator("#name")).to_have_text("Мира, 24")
            await page.locator("#like").click()
            await expect(page.locator("#mutual")).to_be_visible()
            assert not errors
        finally:
            await browser.close()
            await harness.bot.session.close()
