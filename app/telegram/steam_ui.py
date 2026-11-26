from datetime import datetime

from app.persistence.models import utcnow


def steam_status_text(profile):
    count = profile.get("steam_game_count", 0)
    status = profile.get("steam_import_status", "not_loaded")
    if status == "library":
        return f"Из библиотеки Steam сохранено игр: {count}." if count else "Steam вернул пустую библиотеку."
    if status == "recent":
        return f"Из Steam загружены недавние игры: {count}. Полная библиотека не получена."
    if status == "profile_only":
        text = "Последняя проверка не загрузила список игр Steam."
        if count:
            text += f" Сохранено игр из предыдущей загрузки: {count}."
        return text
    if count:
        return f"Из Steam сохранено игр: {count}."
    return "Игры из Steam ещё не загружены."


async def steam_attempt_allowed(message, state):
    data = await state.get_data()
    if (
        data.get("steam_attempt_at")
        and (utcnow() - datetime.fromisoformat(data["steam_attempt_at"])).total_seconds() < 10
    ):
        await message.answer("Подожди несколько секунд перед следующим запросом Steam.")
        return False
    await state.update_data(steam_attempt_at=utcnow().isoformat())
    return True
