from aiogram.fsm.state import State, StatesGroup


# Keep Form and its state names stable for saved Telegram drafts.
class Form(StatesGroup):
    consent = State()
    step = State()
    games = State()
    game_source = State()
    registration_steam = State()
    steam_link = State()
    photo = State()
    review = State()
    focus = State()


class FortniteForm(StatesGroup):
    nickname = State()
