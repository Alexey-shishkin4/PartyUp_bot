import json

from sqlalchemy import create_engine, event, func, inspect, select, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from .config import ROOT
from .models import Base, Game, ProfileSequence, TelegramBan, User


class Database:
    def __init__(self, url: str):
        kwargs = {}
        if url.startswith("sqlite"):
            kwargs["connect_args"] = {"check_same_thread": False, "timeout": 20}
            if ":memory:" in url:
                kwargs["poolclass"] = StaticPool
        self.engine = create_engine(url, **kwargs)
        if url.startswith("sqlite"):
            @event.listens_for(self.engine, "connect")
            def configure_sqlite(connection, _):
                connection.execute("PRAGMA foreign_keys=ON")
                connection.execute("PRAGMA journal_mode=WAL")
        self.session = sessionmaker(self.engine, expire_on_commit=False)

    def initialize(self):
        Base.metadata.create_all(self.engine)
        # Preserve registered users and drafts while upgrading older MVP databases.
        existing_columns = {column["name"] for column in inspect(self.engine).get_columns("users")}
        additions = {
            "share_username": "ALTER TABLE users ADD COLUMN share_username BOOLEAN NOT NULL DEFAULT 0",
            "looking_for_game": "ALTER TABLE users ADD COLUMN looking_for_game INTEGER",
            "looking_until": "ALTER TABLE users ADD COLUMN looking_until DATETIME",
            "notify_new_profiles": "ALTER TABLE users ADD COLUMN notify_new_profiles BOOLEAN NOT NULL DEFAULT 0",
            "steam_verified": "ALTER TABLE users ADD COLUMN steam_verified BOOLEAN NOT NULL DEFAULT 0",
            "photo_file_id": "ALTER TABLE users ADD COLUMN photo_file_id VARCHAR(1024)",
            "steam_import_status": "ALTER TABLE users ADD COLUMN steam_import_status VARCHAR(24) NOT NULL DEFAULT 'not_loaded'",
            "is_test": "ALTER TABLE users ADD COLUMN is_test BOOLEAN NOT NULL DEFAULT 0",
            "test_photo_override": "ALTER TABLE users ADD COLUMN test_photo_override BOOLEAN NOT NULL DEFAULT 0",
        }
        with self.engine.begin() as connection:
            for column, statement in additions.items():
                if column not in existing_columns:
                    connection.execute(text(statement))
        with self.session.begin() as session:
            sequence = session.get(ProfileSequence, 1)
            next_id = (session.scalar(select(func.max(User.id))) or 0) + 1
            if sequence is None:
                session.add(ProfileSequence(id=1, next_id=next_id))
            else:
                sequence.next_id = max(sequence.next_id, next_id)
            # Preserve bans from installations created before telegram_bans.
            for telegram_id in session.scalars(select(User.telegram_id).where(User.banned.is_(True))):
                if session.get(TelegramBan, telegram_id) is None:
                    session.add(TelegramBan(telegram_id=telegram_id))
            for row in json.loads((ROOT / "data/catalog.json").read_text(encoding="utf-8")):
                game = session.get(Game, row["app_id"])
                if game is None:
                    session.add(Game(**row))
                else:
                    for field, value in row.items():
                        setattr(game, field, value)
        # create_all skips indexes of existing tables; upgrade them explicitly.
        for table in (User.__table__, Base.metadata.tables["events"]):
            for index in table.indexes:
                index.create(self.engine, checkfirst=True)

    def catalog(self):
        with self.session() as session:
            return list(session.scalars(select(Game).order_by(Game.name)))
