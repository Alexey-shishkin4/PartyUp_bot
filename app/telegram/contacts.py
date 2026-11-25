import re

from app.domain.profiles import PHOTO_UNCHANGED


async def current_username(bot, peer, known=PHOTO_UNCHANGED):
    if not peer.share_username:
        return None
    if known is PHOTO_UNCHANGED:
        known = (await bot.get_chat(peer.telegram_id, request_timeout=5)).username
    return known if re.fullmatch(r"[A-Za-z0-9_]{1,64}", known or "") else None
