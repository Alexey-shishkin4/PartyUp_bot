from sqlalchemy import select

from app.domain.profiles import ProfileInput
from app.persistence.models import DeveloperAudit, User

from .fixture_catalog import fixture_definitions, fixture_photo


def seed_test_profiles(services):
    # Retire removed examples without touching real profiles or deleting history.
    with services.db.session.begin() as session:
        for user in session.scalars(
            select(User).where(User.is_test.is_(True), User.telegram_id.not_in(fixture_definitions()))
        ):
            manually_edited = session.scalar(
                select(DeveloperAudit.id)
                .where(
                    DeveloperAudit.target_user_id == user.id,
                    DeveloperAudit.action.in_(["profile_update", "photo_update", "games_update"]),
                )
                .limit(1)
            )
            if manually_edited:
                continue
            user.active = False
            user.photo_file_id = None
    for telegram_id, row in fixture_definitions().items():
        existing = services.profiles.user_by_telegram(telegram_id)
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
                {
                    "appid": game["app_id"],
                    "name": game.get("name", f"Steam {game['app_id']}"),
                    "playtime_forever": game["hours"] * 60,
                    "playtime_2weeks": game.get("recent_hours", 0) * 60,
                }
                for game in row.get("library", [])
            ]
            profile = ProfileInput.model_validate(
                {
                    "name": row["name"],
                    "age": row["age"],
                    "gender": row.get("gender", "female"),
                    "timezone": row["timezone"],
                    "start_hour": row["start_hour"],
                    "end_hour": row["end_hour"],
                    "bio": row["bio"],
                    "game_ids": row.get("game_ids", []),
                },
                context={"allow_empty_games": bool(games)},
            )
            user_id = services.profiles.save_profile(
                telegram_id, profile, test_profile=True, test_library=games
            )
        # Fixture assets may have been replaced; re-upload their current bytes on
        # first display instead of reusing a Telegram file_id for the old portrait.
        if not existing or not existing.test_photo_override:
            services.profiles.set_photo(user_id, None)
