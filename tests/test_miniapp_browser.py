import json
import os

from aiohttp.test_utils import TestServer
from playwright.async_api import async_playwright, expect

from app.miniapp import create_web_app
from tests.telegram.harness import Harness
from tests.test_miniapp import signed


async def test_mobile_and_desktop_miniapp(service, users):
    a, b = users(501), users(502)
    harness = Harness(service)
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
            page.on("pageerror", lambda error: errors.append(str(error)))
            await page.route("https://telegram.org/js/telegram-web-app.js",
                             lambda route: route.fulfill(body=sdk, content_type="application/javascript"))
            await page.goto(str(server.make_url("/miniapp")))
            await expect(page.locator("#name")).to_contain_text(b.name)
            assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            await page.locator("#like").click()
            await expect(page.locator("#status")).to_contain_text("обмен")
            await page.locator("[data-tab=profile]").click()
            await page.locator("#nickname").fill("Мобильный игрок")
            await page.locator("#share").check()
            await page.locator("#editor button").click()
            await expect(page.locator("#status")).to_have_text("Анкета сохранена.")
            await page.locator("#pass").click()
            await expect(page.locator("#empty")).to_be_visible()
            await page.locator("#undo-button").click()
            await expect(page.locator("#player")).to_be_visible()
            await page.locator("#like").click()
            await expect(page.locator("#mutual")).to_be_visible()
            await page.locator("#show-matches").click()
            await page.get_by_role("button", name="Показать контакт").click()
            await expect(page.get_by_role("link", name="@current502")).to_have_attribute("href", "https://t.me/current502")
            await page.reload()
            await page.locator("[data-tab=profile]").click()
            await expect(page.locator("#nickname")).to_have_value("Мобильный игрок")
            await page.set_viewport_size({"width": 1280, "height": 860})
            await page.goto(str(server.make_url("/miniapp/demo")))
            await expect(page.locator("#name")).to_have_text("Мира, 24")
            await page.locator("#like").click()
            await expect(page.locator("#mutual")).to_be_visible()
            assert not errors
        finally:
            await browser.close()
            await harness.bot.session.close()
