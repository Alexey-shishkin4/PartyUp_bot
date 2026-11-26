import json

from sqlalchemy import URL, create_engine, inspect, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

from app.config import RESOURCES

from .models import Base, Game


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
        with self.engine.begin() as connection:
            # Serialize first-time schema creation for this application and schema.
            connection.exec_driver_sql(
                "SELECT pg_advisory_xact_lock(hashtext(current_schema()), hashtext('partyup'))"
            )
            Base.metadata.create_all(connection)
            faceit_columns = {column["name"] for column in inspect(connection).get_columns("faceit_profiles")}
            if not {"steam_id", "source"} <= faceit_columns:
                connection.exec_driver_sql(
                    "ALTER TABLE faceit_profiles "
                    "ADD COLUMN IF NOT EXISTS steam_id VARCHAR(20), "
                    "ADD COLUMN IF NOT EXISTS source VARCHAR(16) NOT NULL DEFAULT 'faceit'"
                )
            connection.execute(
                insert(Game).values(catalog).on_conflict_do_nothing(index_elements=[Game.app_id])
            )

    def catalog(self):
        with self.session() as session:
            return list(session.scalars(select(Game).order_by(Game.name)))
