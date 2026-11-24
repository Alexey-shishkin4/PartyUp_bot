import os
from uuid import uuid4

import pytest
from sqlalchemy.schema import CreateSchema, DropSchema

from app.domain.profiles import ProfileInput
from app.persistence.database import Database
from app.services.application import Services


@pytest.fixture(scope="session")
def postgres_engine():
    url = os.environ.get("PARTYUP_TEST_DATABASE_URL")
    if not url:
        pytest.skip("Укажите PARTYUP_TEST_DATABASE_URL для тестов PostgreSQL")
    engine = Database(url).engine
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture
def postgres_url(postgres_engine):
    schema = f"partyup_test_{uuid4().hex}"
    with postgres_engine.begin() as connection:
        connection.execute(CreateSchema(schema))
    try:
        yield postgres_engine.url.update_query_dict({"options": f"-csearch_path={schema}"})
    finally:
        with postgres_engine.begin() as connection:
            connection.execute(DropSchema(schema, cascade=True))


@pytest.fixture
def service(postgres_url):
    db = Database(postgres_url)
    try:
        db.initialize()
        yield Services(db, include_test_profiles=True)
    finally:
        db.engine.dispose()


@pytest.fixture
def users(service):
    def create(telegram, **changes):
        values = {
            "name": f"Игрок {telegram}",
            "age": 25,
            "game_ids": [105600, 548430],
            "timezone": "Asia/Novosibirsk",
            "start_hour": 20,
            "end_hour": 24,
        }
        values.update(changes)
        user_id = service.profiles.save_profile(
            telegram, ProfileInput(**values), username=f"player{telegram}"
        )
        return service.profiles.user(user_id)

    return create
