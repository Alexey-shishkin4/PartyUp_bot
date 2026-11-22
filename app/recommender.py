"""Объяснимое сходство игр + обученные item-item связи + жанры + расписание."""
import json
import math
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo

from .models import Game, User

VERSION = "hybrid-cosine-v1"


@lru_cache(maxsize=16384)
def title_key(value: str) -> str:
    return re.sub(r"[^\w]+", " ", value.casefold()).strip()


class AffinityModel:
    def __init__(self, path: Path):
        self.data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        self.neighbors = self.data.get("neighbors", {})
        self.version = self.data.get("version", "content-only")
        # Serving treats the graph symmetrically. Build incoming edges once,
        # instead of comparing every pair of games for every candidate.
        self.links = defaultdict(dict)
        for source, neighbors in self.neighbors.items():
            for target, value in neighbors.items():
                weight = max(value, self.links[source].get(target, 0))
                self.links[source][target] = self.links[target][source] = weight

    def similarity(self, left: str, right: str) -> float:
        a, b = title_key(left), title_key(right)
        if a == b:
            return 1.0
        return max(self.neighbors.get(a, {}).get(b, 0), self.neighbors.get(b, {}).get(a, 0))

    def game_similarity(self, left, right):
        return max(self.similarity(a, b) for a in [left.name, *left.aliases]
                   for b in [right.name, *right.aliases])

    def library_similarity(self, source, target):
        if not source.weights or not target.weights:
            return 0.0
        total = 0.0
        for app_id, weight in source.weights.items():
            keys = source.keys[app_id]
            if keys & target.titles:
                best = 1.0
            else:
                best = max((value for key in keys for neighbor, value in self.links.get(key, {}).items()
                            if neighbor in target.titles), default=0.0)
            total += weight * best
        return total / sum(source.weights.values())


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


def schedule_slots(user: User, now: datetime) -> frozenset[int]:
    """Daily local intervals converted at each actual UTC date (including DST).

    Half-hour resolution handles non-integer time-zone offsets. An interval that
    crosses midnight belongs to both local dates; the schedule repeats daily.
    """
    base = now.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    return _schedule_slots(user.timezone, user.start_hour, user.end_hour, base)


@lru_cache(maxsize=4096)
def _schedule_slots(zone_name, start, end, base):
    slots = set()
    zone = ZoneInfo(zone_name)
    for index in range(7 * 48):
        local = (base + timedelta(minutes=30 * index)).astimezone(zone)
        hour = local.hour + local.minute / 60
        inside = start <= hour < end if start < end else hour >= start or hour < end
        if inside:
            slots.add(index)
    return frozenset(slots)


@dataclass(frozen=True)
class ProfileFeatures:
    weights: dict[int, float]
    games: dict[int, Game]
    keys: dict[int, frozenset[str]]
    titles: frozenset[str]
    genres: dict[str, float]
    slots: frozenset[int]


def prepare(user: User, now: datetime) -> ProfileFeatures:
    game_weights = weights(user)
    games = {entry.app_id: entry.game for entry in user.games}
    keys = {app_id: frozenset(title_key(title) for title in [games[app_id].name, *games[app_id].aliases])
            for app_id in game_weights}
    genres = defaultdict(float)
    for app_id, weight in game_weights.items():
        labels = games[app_id].genres
        for genre in labels:
            genres[genre] += weight / max(1, len(labels))
    norm = math.sqrt(sum(value * value for value in genres.values())) or 1
    return ProfileFeatures(game_weights, games, keys, frozenset(key for titles in keys.values() for key in titles),
                           {key: value / norm for key, value in genres.items()}, schedule_slots(user, now))


def score(a: User, b: User, model: AffinityModel, now: datetime | None = None, *,
          prepared_a: ProfileFeatures | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    left, right = prepared_a or prepare(a, now), prepare(b, now)
    wa, wb = left.weights, right.weights
    ga = left.games
    exact = sum(value * wb.get(key, 0) for key, value in wa.items())

    # A high affinity must hold in both directions; one broad library cannot dominate.
    affinity = min(model.library_similarity(left, right), model.library_similarity(right, left))
    games_score = 0.85 * exact + 0.15 * affinity

    ta, tb = left.genres, right.genres
    genres_score = sum(value * tb.get(key, 0) for key, value in ta.items())
    sa, sb = left.slots, right.slots
    shared = sa & sb
    overlap = len(shared)
    base = now.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    zone = ZoneInfo(a.timezone)
    local_now = now.astimezone(zone)
    # Choose the next shared window. Group adjacent half-hour slots in UTC,
    # then convert its endpoints to the viewer's time zone.
    current_slot = int((now - base).total_seconds() // 1800)
    first = min((index for index in shared if index >= current_slot), default=None)
    window = None
    if first is not None:
        last = first
        while last + 1 in shared:
            last += 1
        start = (base + timedelta(minutes=30 * first)).astimezone(zone)
        end = (base + timedelta(minutes=30 * (last + 1))).astimezone(zone)
        window = {"start": start.strftime("%H:%M"), "end": end.strftime("%H:%M"),
                  "day_offset": (start.date() - local_now.date()).days, "ongoing": start <= now}
    time_score = overlap / max(len(sa), len(sb), 1)
    common = sorted(wa.keys() & wb.keys(), key=lambda key: (-wa[key] * wb[key], key))
    total = 0.65 * games_score + 0.25 * genres_score + 0.10 * time_score
    return {
        "score": round(100 * total, 1),
        "components": {"games": round(games_score, 3), "genres": round(genres_score, 3),
                       "time": round(time_score, 3)},
        "common_games": [ga[key].name for key in common[:5]],
        "coop_games": [ga[key].name for key in common if ga[key].coop][:3],
        "common_genres": sorted(ta.keys() & tb.keys()),
        "overlap_hours": round(overlap / 2 / 7, 1),
        "next_overlap": window,
        "algorithm": VERSION,
        "model": model.version,
    }
