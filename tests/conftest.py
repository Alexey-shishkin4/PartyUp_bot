import pytest

from app.config import ROOT
from app.db import Database
from app.recommender import AffinityModel
from app.schemas import ProfileInput
from app.service import Service


@pytest.fixture
def service(tmp_path):
    db = Database("sqlite:///" + (tmp_path / "test.sqlite3").as_posix())
    db.initialize()
    instance = Service(db, AffinityModel(ROOT / "data/affinity.json"))
    yield instance
    db.engine.dispose()


@pytest.fixture
def users(service):
    def create(telegram, **changes):
        values = {"name": f"Игрок {telegram}", "age": 25, "game_ids": [105600, 548430],
                  "timezone": "Asia/Novosibirsk", "start_hour": 20, "end_hour": 24}
        values.update(changes)
        user_id = service.save_profile(telegram, ProfileInput(**values), username=f"player{telegram}")
        return service.user(user_id)
    return create
