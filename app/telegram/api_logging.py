import logging
from time import monotonic

from aiogram.exceptions import ClientDecodeError, TelegramAPIError, TelegramRetryAfter
from aiogram.methods import GetUpdates


log = logging.getLogger("app.api")


async def log_telegram_request(make_request, bot, method):
    operation = type(method).__name__
    start = monotonic()
    try:
        result = await make_request(bot, method)
    except (TelegramAPIError, ClientDecodeError) as error:
        retry = f" retry_after_s={error.retry_after}" if isinstance(error, TelegramRetryAfter) else ""
        log.warning(
            "provider=telegram operation=%s error=%s elapsed_ms=%.0f%s",
            operation, type(error).__name__, (monotonic() - start) * 1000, retry,
        )
        raise
    log.log(
        logging.DEBUG if isinstance(method, GetUpdates) else logging.INFO,
        "provider=telegram operation=%s outcome=ok elapsed_ms=%.0f",
        operation, (monotonic() - start) * 1000,
    )
    return result
