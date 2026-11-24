from html import escape

from aiogram import Router
from aiogram.filters import Command


def build_moderation_router(moderation, settings):
    router = Router(name="moderation")

    @router.message(Command("reports"))
    async def reports(message):
        if message.from_user.id not in settings.admins:
            return
        rows = moderation.pending_reports()
        await message.answer(
            "Нет новых жалоб."
            if not rows
            else "\n".join(f"Жалоба #{row.id}: анкета {row.target_id} · {escape(row.reason)}" for row in rows)
            + "\n/ban ID — заблокировать, /unban ID — разблокировать"
        )

    @router.message(Command("ban", "unban"))
    async def moderate(message, command):
        if message.from_user.id not in settings.admins:
            return
        value = (command.args or "").strip()
        if not value.isascii() or not value.isdecimal() or len(value) > 19 or not 0 < int(value) < 2**63:
            await message.answer("Формат: /ban ID или /unban ID")
            return
        changed = moderation.set_banned(int(value), banned=command.command == "ban")
        await message.answer("Статус обновлён." if changed else "Анкета не найдена.")

    return router
