import asyncio
import logging

from aiogram.exceptions import TelegramAPIError, TelegramForbiddenError
from aiogram.types import BotCommand, BotCommandScopeChat

log = logging.getLogger(__name__)


async def deliver_profile_alerts(bot, discovery):
    for user_id, telegram_id in discovery.pending_profile_alerts():
        user = discovery.profiles.user(user_id)
        if not user or not user.notify_new_profiles:
            continue
        if not discovery.feed(user_id, record_impressions=False, real_only=True, availability_only=True):
            continue
        try:
            await bot.send_message(
                telegram_id, "В твоём поиске появилась подходящая анкета. Открыть подборку — /find."
            )
        except TelegramForbiddenError:
            pass
        except TelegramAPIError as error:
            log.info("Could not deliver a discovery notification: %s", type(error).__name__)
            continue
        discovery.set_profile_alert(user_id, False)


async def profile_alerts(bot, discovery):
    while True:
        await asyncio.sleep(90)
        await deliver_profile_alerts(bot, discovery)


async def run_polling(bot, dispatcher, settings, discovery):
    commands = [
        BotCommand(command="find", description="Найти своих"),
        BotCommand(command="play", description="Найти компанию для игры сейчас"),
        BotCommand(command="profile", description="Моя анкета"),
        BotCommand(command="photo", description="Добавить или изменить фото анкеты"),
        BotCommand(command="matches", description="Взаимные симпатии"),
        BotCommand(command="steam", description="Подключить Steam"),
        BotCommand(command="fortnite", description="Статистика Fortnite"),
        BotCommand(command="games", description="Выбрать актуальные игры"),
        BotCommand(command="edit", description="Изменить общие настройки"),
        BotCommand(command="pause", description="Пауза или возобновление"),
        BotCommand(command="privacy", description="Данные и приватность"),
        BotCommand(command="alerts", description="Уведомление о новых анкетах"),
        BotCommand(command="delete", description="Удалить мои данные"),
        BotCommand(command="cancel", description="Вернуться в меню"),
        BotCommand(command="id", description="Мой Telegram ID"),
    ]
    if settings.enable_test_profiles:
        commands.append(BotCommand(command="test", description="Тестовые анкеты с фотографиями"))
    await bot.set_my_commands(commands)
    for developer_id in settings.developers:
        try:
            await bot.set_my_commands(
                [*commands, BotCommand(command="dev", description="Панель разработчика")],
                scope=BotCommandScopeChat(chat_id=developer_id),
            )
        except TelegramAPIError:
            log.warning("Could not set private developer command menu")
    alerts_task = asyncio.create_task(profile_alerts(bot, discovery))
    try:
        await dispatcher.start_polling(
            bot, close_bot_session=False, allowed_updates=dispatcher.resolve_used_update_types()
        )
    finally:
        alerts_task.cancel()
        await asyncio.gather(alerts_task, return_exceptions=True)
