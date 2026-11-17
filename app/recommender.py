"""Объяснимое сходство игр + обученные item-item связи + жанры + расписание."""
import json
import math
import re
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from .models import User

VERSION = "hybrid-cosine-v1"


def title_key(value: str) -> str:
    return re.sub(r"[^\w]+", " ", value.casefold()).strip()


class AffinityModel:
    def __init__(self, path: Path):
        self.data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        self.neighbors = self.data.get("neighbors", {})
        self.version = self.data.get("version", "content-only")

    def similarity(self, left: str, right: str) -> float:
        a, b = title_key(left), title_key(right)
        if a == b:
            return 1.0
        return max(self.neighbors.get(a, {}).get(b, 0), self.neighbors.get(b, {}).get(a, 0))

    def game_similarity(self, left, right):
        return max(self.similarity(a, b) for a in [left.name, *left.aliases]
                   for b in [right.name, *right.aliases])


def compatible(a: User, b: User) -> bool:
    return (
        a.id != b.id and a.active and b.active and not a.banned and not b.banned
        and a.is_demo == b.is_demo
        and a.language == b.language
        and a.age_min <= b.age <= a.age_max and b.age_min <= a.age <= b.age_max
        and (a.looking_for == "any" or a.looking_for == b.gender)
        and (b.looking_for == "any" or b.looking_for == a.gender)
        and (a.purpose == b.purpose or a.purpose == "both" or b.purpose == "both")
    )


def weights(user: User) -> dict[int, float]:
    raw = {}
    for entry in user.games:
        if not entry.selected and entry.minutes <= 0 and entry.recent_minutes <= 0:
            continue
        raw[entry.app_id] = (
            (3.0 if entry.selected else 0)
            + min(math.log1p(entry.minutes / 60), 6) / 6
            + 1.5 * min(math.log1p(entry.recent_minutes / 60), 4) / 4
        )
    norm = math.sqrt(sum(value * value for value in raw.values())) or 1
    return {key: value / norm for key, value in raw.items()}


def schedule_slots(user: User, now: datetime) -> set[int]:
    """Daily local intervals converted at each actual UTC date (including DST).

    Half-hour resolution handles non-integer time-zone offsets. An interval that
    crosses midnight belongs to both local dates; the schedule repeats daily.
    """
    base = now.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    slots = set()
    zone = ZoneInfo(user.timezone)
    for index in range(7 * 48):
        local = (base + timedelta(minutes=30 * index)).astimezone(zone)
        hour = local.hour + local.minute / 60
        start, end = user.start_hour, user.end_hour
        inside = start <= hour < end if start < end else hour >= start or hour < end
        if inside:
            slots.add(index)
    return slots


def score(a: User, b: User, model: AffinityModel, now: datetime | None = None) -> dict:
    wa, wb = weights(a), weights(b)
    exact = sum(value * wb.get(key, 0) for key, value in wa.items())
    ga = {entry.app_id: entry.game for entry in a.games}
    gb = {entry.app_id: entry.game for entry in b.games}

    def learned(source, target, source_games, target_games):
        if not source or not target:
            return 0.0
        return sum(
            weight * max(
                (model.game_similarity(source_games[i], target_games[j]) for j in target), default=0
            )
            for i, weight in source.items()
        ) / sum(source.values())

    # A high affinity must hold in both directions; one broad library cannot dominate.
    affinity = min(learned(wa, wb, ga, gb), learned(wb, wa, gb, ga))
    games_score = 0.85 * exact + 0.15 * affinity

    def genre_vector(game_weights, game_map):
        result = defaultdict(float)
        for app_id, weight in game_weights.items():
            genres = game_map[app_id].genres
            for genre in genres:
                result[genre] += weight / max(1, len(genres))
        norm = math.sqrt(sum(v * v for v in result.values())) or 1
        return {key: value / norm for key, value in result.items()}

    ta, tb = genre_vector(wa, ga), genre_vector(wb, gb)
    genres_score = sum(value * tb.get(key, 0) for key, value in ta.items())
    now = now or datetime.now(timezone.utc)
    sa, sb = schedule_slots(a, now), schedule_slots(b, now)
    overlap = len(sa & sb)
    time_score = overlap / max(len(sa), len(sb), 1)
    common = sorted(wa.keys() & wb.keys(), key=lambda key: wa[key] * wb[key], reverse=True)
    total = 0.65 * games_score + 0.25 * genres_score + 0.10 * time_score
    return {
        "score": round(100 * total, 1),
        "components": {"games": round(games_score, 3), "genres": round(genres_score, 3),
                       "time": round(time_score, 3)},
        "common_games": [ga[key].name for key in common[:5]],
        "coop_games": [ga[key].name for key in common if ga[key].coop][:3],
        "common_genres": sorted(ta.keys() & tb.keys()),
        "overlap_hours": round(overlap / 2 / 7, 1),
        "algorithm": VERSION,
        "model": model.version,
    }
