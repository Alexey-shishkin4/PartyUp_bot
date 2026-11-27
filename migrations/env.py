from alembic import context
from sqlalchemy import create_engine, pool

from app.config import Settings
from app.persistence.models import Base


def migrate(connection):
    context.configure(connection=connection, target_metadata=Base.metadata)
    with context.begin_transaction():
        connection.exec_driver_sql(
            "SELECT pg_advisory_xact_lock(hashtext(current_schema()), hashtext('partyup'))"
        )
        context.run_migrations()


if context.is_offline_mode():
    context.configure(
        url=Settings().db_url,
        target_metadata=Base.metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()
elif "connection" in context.config.attributes:
    migrate(context.config.attributes["connection"])
else:
    engine = create_engine(Settings().db_url, poolclass=pool.NullPool)
    try:
        with engine.connect() as connection:
            migrate(connection)
    finally:
        engine.dispose()
