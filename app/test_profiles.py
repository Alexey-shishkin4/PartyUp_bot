"""Fictional profiles and libraries for testing the feed."""
import json
from functools import lru_cache

from sqlalchemy import select

from .config import ROOT
from .schemas import ProfileInput
from .models import DeveloperAudit,User


@lru_cache(maxsize=1)
def fixture_definitions():
    return {row["telegram_id"]:row for row in json.loads(
        (ROOT / "data/test_profiles.json").read_text(encoding="utf-8"))}


def fixture_photo(telegram_id):
    row = fixture_definitions().get(telegram_id)
    if not row:
        return None
    directory = (ROOT / "data/test_profiles").resolve()
    path = (directory / row.get("photo", row["slug"] + ".jpg")).resolve()
    return path if path.parent == directory and path.is_file() else None


def seed_test_profiles(service):
    # Retire removed examples without touching real profiles or deleting history.
    with service.db.session.begin() as session:
        for user in session.scalars(select(User).where(
            User.is_test.is_(True),User.telegram_id.not_in(fixture_definitions())
        )):
            manually_edited=session.scalar(select(DeveloperAudit.id).where(
                DeveloperAudit.target_user_id==user.id,
                DeveloperAudit.action.in_(["profile_update","photo_update","games_update"])
            ).limit(1))
            if manually_edited:
                continue
            user.active = False
            user.photo_file_id = None
    for telegram_id,row in fixture_definitions().items():
        existing = service.user_by_telegram(telegram_id)
        if existing and existing.banned:
            continue
        if fixture_photo(telegram_id) is None:
            raise RuntimeError("Missing test-profile photo; check the fixture asset directory")
        # Existing fixtures can be edited in the developer panel; retain those
        # fields across restarts rather than restoring the manifest defaults.
        if existing:
            user_id = existing.id
        else:
            games = [
                {"appid": game["app_id"], "name": game.get("name", f"Steam {game['app_id']}"),
                 "playtime_forever": game["hours"] * 60,
                 "playtime_2weeks": game.get("recent_hours", 0) * 60}
                for game in row.get("library", [])
            ]
            profile = ProfileInput.model_validate({
                "name": row["name"], "age": row["age"], "gender": row.get("gender", "female"),
                "timezone": row["timezone"], "start_hour": row["start_hour"], "end_hour": row["end_hour"],
                "bio": row["bio"], "game_ids": row.get("game_ids", []),
            }, context={"allow_empty_games": bool(games)})
            user_id = service.save_profile(telegram_id, profile, test_profile=True, test_library=games)
        # Fixture assets may have been replaced; re-upload their current bytes on
        # first display instead of reusing a Telegram file_id for the old portrait.
        if not existing or not existing.test_photo_override:
            service.set_photo(user_id,None)
