"""Read-only Mini App prototype backed by the team's fictional fixtures."""
import json
from functools import lru_cache

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from .config import ROOT
from .recommender import steam_card_game_ids
from .test_profiles import fixture_definitions, fixture_photo

router = APIRouter(prefix="/miniapp", include_in_schema=False)
DECK = ("mira", "nika", "mark", "asya", "artem", "sonya")


@router.get("")
def index():
    return FileResponse(ROOT / "static/miniapp/index.html")


@router.get("/deck")
@lru_cache(maxsize=1)
def deck():
    catalog = {game["app_id"]: game["name"] for game in
               json.loads((ROOT / "data/catalog.json").read_text(encoding="utf-8"))}
    fixtures = {row["slug"]: row for row in fixture_definitions().values()}
    cards = []
    for slug in DECK:
        row = fixtures[slug]
        library = row.get("library", [])
        ranked = steam_card_game_ids((g["app_id"], g["hours"] * 60, g.get("recent_hours", 0) * 60)
                                     for g in library)
        names = {g["app_id"]: g["name"] for g in library if g.get("name")}
        games = [names.get(i) or catalog.get(i, f"Steam {i}")
                 for i in dict.fromkeys([*ranked, *row.get("game_ids", [])])]
        cards.append({**{k: row[k] for k in ("slug", "name", "age", "timezone", "bio", "start_hour", "end_hour")},
                      "photo": f"/miniapp/portrait/{slug}", "games": games[:5],
                      "incoming_like": slug in ("mira", "mark")})
    return cards


@router.get("/portrait/{slug}")
def portrait(slug: str):
    if slug in DECK:
        for identifier, row in fixture_definitions().items():
            if row["slug"] == slug and (photo := fixture_photo(identifier)):
                return FileResponse(photo)
    raise HTTPException(404, "Анкета отсутствует в макете")
