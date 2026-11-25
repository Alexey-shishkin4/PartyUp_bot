import re


async def current_username(bot, peer):
    username = (await bot.get_chat(peer.telegram_id, request_timeout=5)).username
    return username if re.fullmatch(r"[A-Za-z0-9_]{1,64}", username or "") else None
