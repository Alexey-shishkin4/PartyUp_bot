import json

from alembic import command
from alembic.config import Config
from sqlalchemy import URL, create_engine, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

from app.config import RESOURCES, ROOT

from .models import Game


class Database:
    def __init__(self, url: URL | str):
        url = make_url(url)
        if url.get_backend_name() != "postgresql":
            raise ValueError("PartyUp использует PostgreSQL")
        self.engine = create_engine(
            url.set(drivername="postgresql+psycopg"), pool_pre_ping=True, connect_args={"connect_timeout": 10}
        )
        self.session = sessionmaker(self.engine, expire_on_commit=False)

    def initialize(self):
        catalog = json.loads((RESOURCES / "catalog.json").read_text(encoding="utf-8"))
        catalog = [{"slug": f"steam-{game['app_id']}", **game} for game in catalog]
        with self.engine.begin() as connection:
            config = Config(str(ROOT / "alembic.ini"))
            config.attributes["connection"] = connection
            command.upgrade(config, "head")
            connection.execute(
                insert(Game).values(catalog).on_conflict_do_nothing(index_elements=[Game.app_id])
            )

    def catalog(self):
        with self.session() as session:
            return list(session.scalars(select(Game).order_by(Game.name)))
