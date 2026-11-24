import asyncio
import logging
from functools import partial

import httpx
from aiogram.exceptions import TelegramAPIError, TelegramUnauthorizedError
from aiogram.utils.token import TokenValidationError, validate_token

from app.config import Settings
from app.integrations.opendota import OpenDotaClient
from app.integrations.steam import SteamClient
from app.integrations.steam_public import PublicSteamParser
from app.persistence.database import Database
from app.services.application import Services
from app.services.fixtures import seed_test_profiles
from app.telegram.dispatcher import build_bot
from app.telegram.polling import run_polling

log = logging.getLogger(__name__)


async def run(settings: Settings):
    settings.bot_token = settings.bot_token.strip()
    if not settings.bot_token:
        raise SystemExit("Укажите BOT_TOKEN в .env. Токен выдаёт @BotFather.")
    try:
        validate_token(settings.bot_token)
    except TokenValidationError:
        raise SystemExit("Некорректный BOT_TOKEN. Скопируйте токен целиком из @BotFather.") from None

    if not settings.postgres_password.get_secret_value():
        raise SystemExit("Укажите POSTGRES_PASSWORD в .env.")
    db = Database(settings.db_url)
    try:
        db.initialize()
        services = Services(db, include_test_profiles=settings.enable_test_profiles)
        if settings.enable_test_profiles:
            seed_test_profiles(services)

        async with httpx.AsyncClient(timeout=20, follow_redirects=False) as client:
            steam = SteamClient(settings.steam_api_key, client, OpenDotaClient(client))
            fetch_steam_profile = partial(steam.fetch_profile, public_steam=PublicSteamParser(client))
            bot, dispatcher = build_bot(services, settings, fetch_steam_profile)
            try:
                me = await bot.me()
                log.info("PartyUp запущен как @%s", me.username)
                await run_polling(bot, dispatcher, settings, services.discovery)
            except TelegramUnauthorizedError:
                raise SystemExit("Telegram отклонил BOT_TOKEN. Проверьте токен в @BotFather.") from None
            except TelegramAPIError as error:
                raise SystemExit(f"Не удалось запустить Telegram polling ({type(error).__name__}).") from None
            finally:
                await bot.session.close()
    finally:
        db.engine.dispose()


def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    # HTTP request URLs can include the Steam API key.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    try:
        asyncio.run(run(Settings()))
    except KeyboardInterrupt:
        log.info("PartyUp остановлен")


if __name__ == "__main__":
    main()
