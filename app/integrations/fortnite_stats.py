import re
from collections import defaultdict

from app.domain.fortnite import Activity, EpicAccount, Playtime, Rank

ACCOUNT_ID = re.compile(r"[a-fA-F0-9]{32}")
STAT_KEY = re.compile(
    r"br_(minutesplayed|matchesplayed)_(keyboardmouse|gamepad|touch|all)_m0_playlist_([a-z0-9_]+)",
    re.I,
)
RANK_MODES = {
    "ranked-br": "battle_royale",
    "ranked-br-combined": "battle_royale",
    "ranked-zb": "zero_build",
    "ranked_blastberry_build": "reload",
    "ranked_blastberry_nobuild": "reload_zero_build",
    "ranked-blastberry-combined": "reload_combined",
    "ranked-figment-build": "og",
    "ranked-figment-nobuild": "og_zero_build",
    "ranked-feral": "ballistic",
    "delmar-competitive": "rocket_racing",
    "ranked-squareclub": "arenas",
    "ranked-pimlico": "crown_jam",
    "ranked-bling": "getaway",
    "ranked-bling-nobuild": "getaway_zero_build",
}


def mapping(value):
    return value if isinstance(value, dict) else {}


def count(value):
    return value if type(value) is int and 0 <= value < 2**31 else None


def account(account_id, display_name, fallback_name=None):
    name = display_name or fallback_name
    if isinstance(account_id, str) and ACCOUNT_ID.fullmatch(account_id) and isinstance(name, str):
        name = name.strip()
        if name and name.isprintable() and len(name) <= 64:
            return EpicAccount(account_id.lower(), name)
    return None


def osirion_account(payload, nickname):
    found = payload.get("accounts")
    found = found if isinstance(found, list) and found else [payload]
    for entry in found:
        entry = mapping(entry)
        result = account(entry.get("accountId"), mapping(entry.get("accountDetails")).get("epic"), nickname)
        if result:
            return result
    return None


def osirion_playtime(payload):
    groups = mapping(payload.get("groupedStats"))
    if not groups:
        return None
    inputs = [groups["all"]] if "all" in groups else groups.values()
    by_size = defaultdict(int)
    total = []
    for item in inputs:
        sizes = mapping(item)
        overall = count(mapping(sizes.get("overall") or sizes.get("all")).get("minutesplayed"))
        buckets = {}
        for size, metrics in sizes.items():
            minutes = count(mapping(metrics).get("minutesplayed"))
            if minutes is not None and size not in {"overall", "all"}:
                buckets[size] = minutes
                if re.fullmatch(r"[a-z0-9_-]{1,30}", size):
                    by_size[f"team:{size}"] += minutes
        if overall is not None or buckets:
            total.append(overall if overall is not None else sum(buckets.values()))
    return Playtime(sum(total), dict(by_size), "osirion") if total else None


def osirion_ranks(payload):
    rows = payload.get("modes")
    if not isinstance(rows, list):
        return None
    result = {}
    # Prefer the combined BR track when both BR tracks are present.
    for row in sorted(map(mapping, rows), key=lambda row: row.get("rankingType") == "ranked-br-combined"):
        track = row.get("rankingType")
        if not isinstance(track, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,56}", track):
            continue
        mode = RANK_MODES.get(track, f"rank:{track.lower()}")
        name = mapping(row.get("currentDivision")).get("divisionName")
        season = row.get("rankingTrackId")
        rank = Rank(
            name[:64] if isinstance(name, str) and name.strip() else None,
            season[:64] if isinstance(season, str) else None,
            "osirion",
        )
        if rank.name or mode not in result:
            result[mode] = rank
    return result


def playlist_mode(playlist):
    reload_playlist = playlist.removeprefix("habanero_").removeprefix("nobuild_")
    if reload_playlist.startswith(("blastberry", "reload", "blendheaded", "sourspawn", "timberstake")):
        return "reload_zero_build" if "nobuild" in playlist or "zerobuild" in playlist else "reload"
    if playlist.startswith(("figment", "fortniteog")):
        return "og_zero_build" if "nobuild" in playlist or "zerobuild" in playlist else "og"
    if playlist.startswith(("bling", "thegetaway")):
        return "getaway_zero_build" if "nobuild" in playlist or "zerobuild" in playlist else "getaway"
    if playlist.startswith("squareclub"):
        return "arenas"
    if playlist.startswith("pimlico"):
        return "crown_jam"
    if playlist.startswith(("feral", "ballistic")):
        return "ballistic"
    if playlist.startswith("delmar"):
        return "rocket_racing"
    if playlist.startswith(("radianttoothpick", "blitz")):
        return "blitz"
    if playlist.startswith(("nobuildbr", "zerobuild", "showdownnobuild", "showdownalt_nobuild")):
        return "zero_build"
    if re.fullmatch(r"default(?:solo|duo|trio|squad)s?", playlist) or playlist.startswith(
        ("showdownalt", "rankedbr")
    ):
        return "battle_royale"
    # Keep separately reported time, but avoid attributing it to a ranked track.
    return f"playlist:{playlist}" if len(playlist) <= 55 else None


def raw_activity(payload):
    body = mapping(payload.get("data")) or payload
    stats = body.get("stats", body)
    if not isinstance(stats, dict):
        return None
    by_playlist = defaultdict(dict)
    for key, value in stats.items():
        match = STAT_KEY.fullmatch(key)
        number = count(value)
        if match and number is not None:
            metric, input_type, playlist = match.groups()
            by_playlist[metric.lower(), playlist.lower()][input_type.lower()] = number
    minutes, matches = defaultdict(int), defaultdict(int)
    for (metric, playlist), inputs in by_playlist.items():
        mode = playlist_mode(playlist)
        if mode:
            target = minutes if metric == "minutesplayed" else matches
            target[mode] += inputs["all"] if "all" in inputs else sum(inputs.values())
    if not minutes and not matches and not ("stats" in body and not stats):
        return None
    return Activity(dict(minutes), dict(matches))


def dashboard_stats(payload, nickname=None):
    data = mapping(payload.get("data"))
    player = mapping(data.get("account"))
    resolved = account(player.get("id"), player.get("name"), nickname)
    buckets = mapping(mapping(data.get("stats")).get("all"))
    overall = count(mapping(buckets.get("overall")).get("minutesPlayed"))
    modes = {}
    for size in ("solo", "duo", "trio", "squad", "ltm"):
        minutes = count(mapping(buckets.get(size)).get("minutesPlayed"))
        if minutes is not None:
            modes[f"team:{size}"] = minutes
    return resolved, Playtime(overall, modes, "fortnite-api.com") if overall is not None or modes else None
