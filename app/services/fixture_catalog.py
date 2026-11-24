import json
from functools import lru_cache

from app.config import RESOURCES


@lru_cache(maxsize=1)
def fixture_definitions():
    return {
        row["telegram_id"]: row
        for row in json.loads((RESOURCES / "test_profiles.json").read_text(encoding="utf-8"))
    }


def fixture_photo(telegram_id):
    row = fixture_definitions().get(telegram_id)
    if not row:
        return None
    directory = (RESOURCES / "test_profiles").resolve()
    path = (directory / row.get("photo", row["slug"] + ".jpg")).resolve()
    return path if path.parent == directory and path.is_file() else None
