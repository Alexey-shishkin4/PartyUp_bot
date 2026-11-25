import logging
from datetime import datetime
from html import escape
from zoneinfo import ZoneInfo

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import FSInputFile, InputMediaPhoto, Message

from app.config import RESOURCES
from app.domain.matching import CARD_GAME_LIMIT
from app.domain.profiles import PHOTO_UNCHANGED

from .ui import buttons, clear_buttons

log = logging.getLogger(__name__)
DEFAULT_AVATAR = RESOURCES / "default-avatar.png"


def profile_heading(profile):
    offset = datetime.now(ZoneInfo(profile["timezone"])).strftime("%z")
    minutes = f":{offset[3:]}" if offset[3:] != "00" else ""
    return f"<b>{escape(profile['name'])}</b> (UTC{offset[0]}{int(offset[1:3])}{minutes})"


def card_game_name(name):
    return escape(name[:47] + "…" if len(name) > 48 else name)


def profile_text(profile, *, common_games=()):
    sections = [profile_heading(profile)]
    if profile["games"]:
        names = [card_game_name(name) for name in profile["games"][:CARD_GAME_LIMIT]]
        sections.append("<b>🎮 Игры</b>\n" + "\n".join(f"• {name}" for name in names))
    if common_games:
        # Shared games can also be manually selected, so keep their names in one section.
        sections.append("<b>🤝 Общие игры</b>\n" + " · ".join(map(card_game_name, common_games)))
    extra = [g for g in profile.get("additional_games", []) if g not in common_games]
    if extra:
        sections.append("<b>Выбраны вручную</b>\n" + " · ".join(map(card_game_name, extra)))
    bio = profile["bio"]
    if bio:
        sections.append("<b>О себе</b>\n" + escape(bio))
    return "\n\n".join(sections)


class ProfileCards:
    def __init__(self, profiles):
        self.profiles = profiles

    def photo(self, profile):
        photo = self.profiles.photo_file(profile["id"])
        if not photo and profile.get("use_default_avatar"):
            return FSInputFile(DEFAULT_AVATAR)
        local = self.profiles.test_photo_file(profile["id"]) if profile.get("is_test") and not photo else None
        return FSInputFile(local) if local else photo

    async def send(self, message, profile, text=None, reply_markup=None, *, draft_photo=PHOTO_UNCHANGED):
        text = text or profile_text(profile)
        photo = self.photo(profile) if draft_photo is PHOTO_UNCHANGED else draft_photo
        if photo:
            # Telegram photo captions have a smaller limit than regular messages.
            short = len(text.encode("utf-16-le")) // 2 <= 1024
            caption = text if short else profile_heading(profile)
            try:
                sent = await message.answer_photo(
                    photo, caption=caption, reply_markup=reply_markup if short else None
                )
                if isinstance(photo, FSInputFile) and profile.get("is_test") and sent.photo:
                    self.profiles.cache_test_photo(
                        profile["id"], max(sent.photo, key=lambda item: item.width * item.height).file_id
                    )
                if short:
                    return
            except TelegramBadRequest:
                log.warning("Profile photo could not be sent; using text card")
        await message.answer(text, reply_markup=reply_markup)

    async def display_discovery(self, message, text, rows, *, profile=None, replace=False):
        markup = buttons(rows)
        photo = self.photo(profile) if profile else None
        if photo and len(text.encode("utf-16-le")) // 2 > 1024:
            await self.send(message, profile, text, markup, draft_photo=photo)
            if replace:
                try:
                    await message.delete()
                except TelegramBadRequest:
                    pass
            return
        if replace:
            try:
                if photo:
                    sent = await message.edit_media(
                        InputMediaPhoto(media=photo, caption=text), reply_markup=markup
                    )
                    if (
                        isinstance(photo, FSInputFile)
                        and profile.get("is_test")
                        and isinstance(sent, Message)
                        and sent.photo
                    ):
                        self.profiles.cache_test_photo(profile["id"], sent.photo[-1].file_id)
                    return
                if message.text is not None:
                    await message.edit_text(text, reply_markup=markup)
                    return
            except TelegramBadRequest as error:
                if "message is not modified" in error.message.lower():
                    return
        sent = None
        if photo:
            try:
                sent = await message.answer_photo(photo, caption=text, reply_markup=markup)
            except TelegramBadRequest:
                log.info("Could not send discovery photo; falling back to text")
            if isinstance(photo, FSInputFile) and profile.get("is_test") and sent and sent.photo:
                self.profiles.cache_test_photo(profile["id"], sent.photo[-1].file_id)
        if not sent:
            await message.answer(text, reply_markup=markup)
        if replace:
            try:
                await message.delete()
            except TelegramBadRequest:
                await clear_buttons(message)
