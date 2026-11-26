from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import SimpleEventIsolation

from .cards import ProfileCards, profile_text
from .feed import FeedView
from .routers.common import build_common_router
from .routers.deadlock import build_deadlock_router
from .routers.developer import build_developer_router
from .routers.discovery import build_discovery_router
from .routers.fallback import build_fallback_router
from .routers.fortnite import build_fortnite_router
from .routers.moderation import build_moderation_router
from .routers.profiles import build_profile_router
from .routers.riot import build_riot_router
from .routers.steam import build_steam_router
from .storage import DatabaseStorage
from .ui import menu


def build_bot(
    services, settings, fetch_steam_profile, fetch_fortnite_profile=None, fetch_riot_profile=None,
    *, fetch_deadlock_profile=None,
):
    bot = Bot(settings.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dispatcher = Dispatcher(storage=DatabaseStorage(services.db), events_isolation=SimpleEventIsolation())
    cards = ProfileCards(services.profiles)
    feed_view = FeedView(services.discovery, cards)

    private = Router(name="private")
    private.message.filter(F.chat.type == "private")
    private.callback_query.filter(F.message.chat.type == "private")
    private.include_routers(
        build_developer_router(services, settings, cards.send, profile_text, menu),
        build_common_router(services.profiles, settings),
        build_moderation_router(services.moderation, settings),
        build_profile_router(services.profiles, cards, fetch_steam_profile, feed_view),
        build_steam_router(services.profiles, services.steam, fetch_steam_profile),
        build_discovery_router(services.profiles, services.discovery, cards, feed_view),
    )
    if fetch_fortnite_profile is not None:
        private.include_router(
            build_fortnite_router(services.profiles, services.fortnite, fetch_fortnite_profile)
        )
    if fetch_riot_profile is not None:
        private.include_router(build_riot_router(services.profiles, services.riot, fetch_riot_profile))
    if fetch_deadlock_profile is not None:
        private.include_router(build_deadlock_router(services.profiles, services.deadlock, fetch_deadlock_profile))
    private.include_router(build_fallback_router())
    dispatcher.include_router(private)
    return bot, dispatcher
