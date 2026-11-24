"""Подбор по общим играм, игровому времени и жанрам."""

from __future__ import annotations

import math
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

if TYPE_CHECKING:
    from app.persistence.models import User

VERSION = "steam-playtime-v2"
PLAYTIME_CAP_HOURS = 500
MANUAL_SHARE = 0.10
MANUAL_ONLY_WEIGHT = 0.45
CARD_GAME_LIMIT = 5


def steam_card_game_ids(games):
    """Rank (app ID, lifetime minutes, recent minutes) for display on a card."""

    def rank(game):
        app_id, lifetime, recent = game
        # Recent minutes are a lower bound when Steam omits lifetime playtime.
        lifetime = max(lifetime, recent)
        history = lifetime / (lifetime + 50 * 60)
        activity_bonus = 1 + 2 * recent / (recent + 5 * 60)
        return (-history * activity_bonus, -recent, -lifetime, app_id)

    played = (game for game in games if game[1] > 0 or game[2] > 0)
    return [game[0] for game in sorted(played, key=rank)[:CARD_GAME_LIMIT]]


def compatible(a: User, b: User) -> bool:
    return (
        a.id != b.id
        and a.active
        and b.active
        and not a.banned
        and not b.banned
        and a.language == b.language
        and a.age_min <= b.age <= a.age_max
        and b.age_min <= a.age <= b.age_max
        and (a.looking_for == "any" or a.looking_for == b.gender)
        and (b.looking_for == "any" or b.looking_for == a.gender)
        and (a.purpose == b.purpose or a.purpose == "both" or b.purpose == "both")
    )


def weights(user: User) -> dict[int, float]:
    played = {
        entry.app_id: max(entry.effective_minutes, entry.recent_minutes)
        for entry in user.games
        if entry.effective_minutes > 0 or entry.recent_minutes > 0
    }
    manual = [entry.app_id for entry in user.games if entry.selected and entry.app_id not in played]
    total = sum(played.values())
    manual_share = (MANUAL_SHARE if total else MANUAL_ONLY_WEIGHT) if manual else 0.0
    # Keep absolute playtime in the signal even when the library contains only one game.
    result = {
        app_id: (1 - manual_share)
        * minutes
        / total
        * min(1.0, math.log1p(minutes / 60) / math.log1p(PLAYTIME_CAP_HOURS))
        for app_id, minutes in played.items()
    }
    if manual:
        result.update({app_id: manual_share / len(manual) for app_id in manual})
    return result


def genre_weights(game_weights, games):
    result = defaultdict(float)
    for app_id, weight in game_weights.items():
        genres = set(games[app_id].genres)
        for genre in genres:
            result[genre] += weight / len(genres)
    return result


def schedule_slots(user: User, now: datetime) -> set[int]:
    """Daily local intervals converted at each actual UTC date (including DST).

    Half-hour resolution handles non-integer time-zone offsets. An interval that
    crosses midnight belongs to both local dates; the schedule repeats daily.
    """
    if not user.schedule_set:
        return set()
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


def score(a: User, b: User, now: datetime | None = None) -> dict:
    wa, wb = weights(a), weights(b)
    ga = {entry.app_id: entry.game for entry in a.games}
    gb = {entry.app_id: entry.game for entry in b.games}
    shared_games = {app_id: min(wa[app_id], wb[app_id]) for app_id in wa.keys() & wb.keys()}
    common = sorted(shared_games, key=lambda app_id: (-shared_games[app_id], app_id))
    main_game = common[0] if common else None
    main_score = shared_games.get(main_game, 0.0)
    other_score = sum(shared_games[app_id] for app_id in common[1:])
    ta, tb = genre_weights(wa, ga), genre_weights(wb, gb)
    shared_genres = ta.keys() & tb.keys()
    genres_score = sum(min(ta[genre], tb[genre]) for genre in shared_genres)
    now = now or datetime.now(timezone.utc)
    sa, sb = schedule_slots(a, now), schedule_slots(b, now)
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
        window = {
            "start": start.strftime("%H:%M"),
            "end": end.strftime("%H:%M"),
            "day_offset": (start.date() - local_now.date()).days,
            "ongoing": start <= now,
        }
    time_score = overlap / max(len(sa), len(sb), 1)
    games_score = 0.60 * main_score + 0.30 * other_score
    total = games_score + 0.10 * genres_score
    return {
        "score": round(100 * total, 1),
        "components": {
            "games": round((2 * main_score + other_score) / 3, 3),
            "main_game": round(main_score, 3),
            "other_games": round(other_score, 3),
            "genres": round(genres_score, 3),
            "time": round(time_score, 3),
        },
        "common_games": [ga[key].name for key in common[:5]],
        "coop_games": [ga[key].name for key in common if ga[key].coop][:3],
        "common_genres": sorted(shared_genres, key=lambda genre: (-min(ta[genre], tb[genre]), genre)),
        "overlap_hours": round(overlap / 2 / 7, 1),
        "next_overlap": window,
        "algorithm": VERSION,
        "model": VERSION,
    }
