from html import escape

from .cards import profile_text


def shared_time_text(window):
    if not window:
        return "По указанному расписанию свободные часы не пересекаются."
    day = {0: "сегодня", 1: "завтра"}.get(window["day_offset"], "в ближайшие дни")
    timing = (
        f"сейчас, до {window['end']}" if window["ongoing"] else f"{day}, {window['start']}–{window['end']}"
    )
    return f"По расписанию вы оба свободны {timing} по твоему времени."


class FeedView:
    def __init__(self, discovery, cards):
        self.discovery = discovery
        self.cards = cards

    async def show(self, message, user, *, replace=False, preferred_id=None, allow_undo=False):
        rows = self.discovery.feed(user.id, limit=1, preferred_id=preferred_id)
        focus = self.discovery.current_game(user)
        focus_game = self.discovery.current_game_name(focus) if focus else None
        focus_text = f"🎮 Хочу сыграть в {escape(focus_game)}\n\n" if focus_game else ""
        undo = [("↩ Вернуть предыдущую", "feed:undo")]
        if not rows:
            if not user.active:
                await self.cards.display_discovery(
                    message, "Анкета на паузе.", [[("Возобновить показы", "feed:resume")]], replace=replace
                )
                return
            actions = [
                [("Возраст собеседника", "edit:age_range"), ("Кого показывать", "edit:looking_for")],
                [("Изменить игры", "edit:games"), ("Выбрать игру на сейчас", "feed:focus")],
                undo,
                [
                    ("Отменить уведомление", "feed:alert_off")
                    if user.notify_new_profiles
                    else ("Сообщить, когда появится анкета", "feed:alert_on")
                ],
            ]
            if focus:
                actions.insert(0, [("Вернуться к общему поиску", "focus:clear")])
            await self.cards.display_discovery(
                message, focus_text + "Новых подходящих анкет пока нет.", actions, replace=replace
            )
            return
        row = rows[0]
        profile = row["profile"]
        target = profile["id"]
        text = profile_text(profile, common_games=row["common_games"])
        if focus_game:
            heading = (
                f"⚡ Тоже ищет компанию в {escape(focus_game)}"
                if row["same_game_now"]
                else focus_text.strip()
            )
            if user.schedule_set and profile["schedule_set"]:
                heading += "\n" + shared_time_text(row["next_overlap"])
            text = f"{heading}\n\n{text}"
        actions = []
        if allow_undo:
            actions.append(undo)
        if focus:
            actions.append([("Вернуться к общему поиску", "focus:clear")])
        if not profile["is_test"]:
            actions.append(
                [("Заблокировать", f"react:block:{target}"), ("Пожаловаться", f"react:report:{target}")]
            )
        actions.append([("💜 Нравится", f"react:like:{target}"), ("👎 Не нравится", f"react:skip:{target}")])
        await self.cards.display_discovery(message, text, actions, profile=profile, replace=replace)
