from datetime import datetime, timezone

import pytest
from aiogram import Dispatcher
from aiogram.methods import GetMe, SetMyCommands
from aiogram.types import Chat, Message, Update, User
from sqlalchemy import select

from app.config import Settings
from app.main import run
from app.persistence.database import Database
from app.persistence.models import FSMRecord, Reaction
from app.persistence.models import User as Profile
from app.services.application import Services
from app.services.fixtures import seed_test_profiles
from app.telegram import dispatcher as telegram_dispatcher

from .harness import FakeSession, Harness


@pytest.mark.parametrize(
    "token, error",
    [
        ("", "Укажите BOT_TOKEN"),
        ("invalid", "Некорректный BOT_TOKEN"),
        ("123456789:local-test-token", "Укажите POSTGRES_PASSWORD"),
    ],
)
async def test_invalid_configuration_stops_before_connecting(monkeypatch, token, error):
    def unexpected_database(url):
        pytest.fail("Database should not be created with invalid configuration")

    monkeypatch.setattr("app.main.Database", unexpected_database)
    settings = Settings(_env_file=None, bot_token=token, postgres_password="")
    with pytest.raises(SystemExit, match=error):
        await run(settings)


async def test_entrypoint_registers_commands_and_runs_telegram_polling(postgres_url, monkeypatch):
    class Session(FakeSession):
        closed = False

        async def close(self):
            self.closed = True

        async def make_request(self, bot, method, timeout=None):
            if isinstance(method, GetMe):
                self.calls.append(method)
                return User(id=bot.id, is_bot=True, first_name="PartyUp", username="partyup_check_bot")
            if isinstance(method, SetMyCommands):
                self.calls.append(method)
                return True
            return await super().make_request(bot, method, timeout)

    session = Session()
    bot_class = telegram_dispatcher.Bot

    def fake_bot(*args, **kwargs):
        return bot_class(*args, **kwargs, session=session)

    async def poll(dispatcher, bot, *, close_bot_session, allowed_updates):
        assert not close_bot_session
        assert {"message", "callback_query"} <= set(allowed_updates)
        private_router = next(router for router in dispatcher.sub_routers if router.name == "private")
        assert {"steam", "fortnite", "riot", "faceit", "deadlock", "pubg"} <= {
            router.name for router in private_router.sub_routers
        }
        await dispatcher.feed_update(
            bot,
            Update(
                update_id=1,
                message=Message(
                    message_id=1,
                    date=datetime.now(timezone.utc),
                    chat=Chat(id=1, type="private"),
                    from_user=User(id=1, is_bot=False, first_name="Игрок"),
                    text="/start",
                ),
            ),
        )

    monkeypatch.setattr(telegram_dispatcher, "Bot", fake_bot)
    monkeypatch.setattr(Dispatcher, "start_polling", poll)
    monkeypatch.setattr(Settings, "db_url", property(lambda self: postgres_url))
    await run(
        Settings(
            _env_file=None,
            bot_token="123456789:local-test-token",
            postgres_password="local-test",
            enable_test_profiles=False,
        )
    )

    assert session.closed
    commands = next(call.commands for call in session.calls if isinstance(call, SetMyCommands))
    command_names = {command.command for command in commands}
    assert {"find", "steam", "fortnite", "valorant", "lol", "faceit", "deadlock", "pubg"} <= command_names
    assert "test" not in command_names
    db = Database(postgres_url)
    try:
        with db.session() as session:
            assert session.scalar(select(FSMRecord.state).where(FSMRecord.telegram_id == 1)) == "Form:step"
            assert session.scalar(select(Profile.id).where(Profile.is_test.is_(True)).limit(1)) is None
    finally:
        db.engine.dispose()


async def test_disabled_testing_hides_existing_fixtures_from_feed_and_old_buttons(service, users):
    actor = users(1)
    seed_test_profiles(service)
    fixture = service.discovery.feed(actor.id, limit=1, test_only=True)[0]["profile"]
    production = Services(service.db)
    assert production.discovery.feed(actor.id) == []

    harness = Harness(production)
    try:
        await harness.update(1, "/test")
        assert "Тестовая лента выключена" in harness.sent()[-1].text
        await harness.update(1, callback=f"react:like:{fixture['id']}")
        assert service.profiles.user(fixture["id"]).is_test
        with service.db.session() as db:
            assert db.get(Reaction, (actor.id, fixture["id"])) is None
    finally:
        await harness.bot.session.close()
