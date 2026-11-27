"""Upgrade the PostgreSQL schema that PartyUp created before Alembic."""

import json
from collections import defaultdict
from itertools import groupby

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import insert

from app.config import RESOURCES
from app.domain.deadlock import rank_name as deadlock_rank
from app.domain.dota import DOTA_APP_ID, dota_rank_name
from app.domain.fortnite import favorite_rank_mode
from app.domain.games import FORTNITE_GAME_ID, LEAGUE_GAME_ID, VALORANT_GAME_ID
from app.domain.pubg import profile_rank as pubg_rank
from app.domain.steam import CS2_APP_ID, DEADLOCK_APP_ID, PUBG_APP_ID


CORE_TABLES = {
    "blocks", "developer_audit", "events", "fsm_records", "games",
    "matches", "reactions", "reports", "user_games", "users",
}
LEGACY_GAME_TABLES = (
    "fortnite_mode_stats", "fortnite_accounts", "riot_profiles", "pubg_profiles",
    "faceit_profiles", "deadlock_profiles",
)
STEAM_COLUMNS = ("steam_id", "steam_checked_at", "steam_import_status", "dota_rank_tier")
RIOT_IDS = {"lol": LEAGUE_GAME_ID, "valorant": VALORANT_GAME_ID}


def legacy_tables():
    if op.get_context().as_sql:
        return None
    schema = sa.inspect(op.get_bind())
    existing = set(schema.get_table_names())
    old_games = tuple(table for table in LEGACY_GAME_TABLES if table in existing)
    normalized = existing & {"game_accounts", "game_stats"}
    if not (existing & CORE_TABLES or old_games or normalized):
        return None
    if CORE_TABLES <= existing and not normalized:
        users = {column["name"] for column in schema.get_columns("users")}
        games = {column["name"] for column in schema.get_columns("games")}
        user_games = {column["name"] for column in schema.get_columns("user_games")}
        if (
            set(STEAM_COLUMNS) <= users and {"app_id", "coop"} <= games and "slug" not in games
            and "app_id" in user_games and "game_id" not in user_games
        ):
            return old_games
    raise RuntimeError(
        "Существующая схема PartyUp без ревизии Alembic не соответствует поддерживаемой старой схеме. "
        "Сравните схему с версией бота, создавшей базу."
    )


def prepare_schema():
    connection = op.get_bind()
    op.add_column("games", sa.Column("slug", sa.String(96)))
    op.execute("UPDATE games SET slug = 'steam-' || app_id")
    games = sa.Table("games", sa.MetaData(), autoload_with=connection)
    catalog = json.loads((RESOURCES / "catalog.json").read_text(encoding="utf-8"))
    ranked_ids = {CS2_APP_ID, DOTA_APP_ID, PUBG_APP_ID, DEADLOCK_APP_ID, FORTNITE_GAME_ID, *RIOT_IDS.values()}
    ranked_games = [
        {"slug": f"steam-{game['app_id']}", **game, "coop": False}
        for game in catalog if game["app_id"] in ranked_ids
    ]
    seed = insert(games).values(ranked_games)
    connection.execute(seed.on_conflict_do_update(
        index_elements=[games.c.app_id], set_={"slug": seed.excluded.slug},
    ))
    op.alter_column("games", "slug", nullable=False)
    op.create_unique_constraint("games_slug_key", "games", ["slug"])
    op.alter_column("user_games", "app_id", new_column_name="game_id")
    for column in ("minutes", "recent_minutes", "dota_match_minutes"):
        op.alter_column("user_games", column, type_=sa.BigInteger())


def iso(value):
    return value.isoformat() if value is not None else None


def transfer_accounts(legacy, accounts, stats):
    connection = op.get_bind()
    metadata = sa.MetaData()
    sources = {
        name: sa.Table(name, metadata, autoload_with=connection)
        for name in (*legacy, "users", "user_games")
    }

    def rows(name):
        if name in sources:
            return connection.execute(sa.select(sources[name])).mappings()
        return ()

    def add_account(user_id, platform, external_id, *, checked_at, display_name=None, status="linked", details=None):
        return connection.scalar(accounts.insert().values(
            user_id=user_id, platform=platform, external_account_id=external_id, display_name=display_name,
            checked_at=checked_at, status=status, details=details or {},
        ).returning(accounts.c.id))

    def add_stats(account_id, game_id, *, checked_at, rank, source, scope, details, matches=None, minutes=None):
        connection.execute(stats.insert().values(
            account_id=account_id, game_id=game_id, checked_at=checked_at, rank=rank, source=source, scope=scope,
            details=details, matches_count=matches, playtime_minutes=minutes,
        ))

    deadlock = {row["user_id"]: row for row in rows("deadlock_profiles")}
    user_games = sources["user_games"]
    dota = dict(connection.execute(
        sa.select(user_games.c.user_id, user_games.c.dota_match_minutes).where(user_games.c.game_id == DOTA_APP_ID)
    ).tuples().all())
    for row in rows("users"):
        user_id = row["id"]
        deadlock_row = deadlock.pop(user_id, None)
        steam_id = row["steam_id"]
        if not steam_id:
            if deadlock_row is not None:
                raise RuntimeError("В старой базе есть статистика Deadlock без привязки Steam.")
            continue
        checked = row["steam_checked_at"]
        account = add_account(
            user_id, "steam", steam_id, status=row["steam_import_status"], checked_at=checked,
        )
        tier, minutes = row["dota_rank_tier"], dota.get(user_id) or None
        if tier is not None or minutes is not None:
            add_stats(
                account, DOTA_APP_ID, checked_at=checked or row["last_active"], rank=dota_rank_name(tier),
                source="opendota", scope="available_history", details={"rank_tier": tier}, minutes=minutes,
            )
        if deadlock_row is not None:
            if deadlock_row["steam_id"] != steam_id:
                raise RuntimeError("Привязки Steam и Deadlock в старой базе не совпадают.")
            badge, seconds = deadlock_row["rank_badge"], deadlock_row["match_seconds"]
            add_stats(
                account, DEADLOCK_APP_ID, checked_at=deadlock_row["checked_at"], rank=deadlock_rank(badge),
                source="deadlock", scope="available_history", matches=deadlock_row["match_count"],
                minutes=seconds // 60 if seconds is not None else None,
                details={"rank_badge": badge, "ranked_at": iso(deadlock_row["ranked_at"]), "match_seconds": seconds},
            )

    for row in rows("faceit_profiles"):
        checked = row["checked_at"]
        account = add_account(
            row["user_id"], "faceit", row["player_id"], display_name=row["nickname"], checked_at=checked,
            details={"steam_id": row["steam_id"]} if row.get("steam_id") else {},
        )
        level = row["skill_level"]
        add_stats(
            account, CS2_APP_ID, checked_at=checked, rank=f"FACEIT {level}" if level is not None else None,
            source=row.get("source", "faceit"), scope="lifetime", matches=row["match_count"],
            details={key: row[key] for key in ("elo", "skill_level", "wins", "win_rate")},
        )

    for row in rows("pubg_profiles"):
        checked = max((value for value in (row["playtime_at"], row["rank_at"]) if value is not None), default=None)
        account = add_account(
            row["user_id"], "pubg", row["account_id"], display_name=row["nickname"], checked_at=checked,
            details={"shard": row["platform"]},
        )
        ranks, seconds = row["ranks"] or {}, row["mode_seconds"] or {}
        details = {
            "ranks": ranks, "mode_seconds": seconds, "season_id": row["season_id"],
            "rank_source": row["rank_source"], "playtime_source": row["playtime_source"],
            "rank_at": iso(row["rank_at"]), "playtime_at": iso(row["playtime_at"]),
        }
        add_stats(
            account, PUBG_APP_ID, checked_at=checked or sa.func.now(), rank=pubg_rank(ranks, seconds),
            source=row["rank_source"] or row["playtime_source"],
            scope="ranked_season" if row["season_id"] else "available_history", details=details,
            minutes=sum(seconds.values()) // 60 if row["mode_seconds"] is not None else None,
        )

    modes = defaultdict(list)
    for row in rows("fortnite_mode_stats"):
        modes[row["user_id"]].append(row)
    for row in rows("fortnite_accounts"):
        checked = row["checked_at"]
        account = add_account(
            row["user_id"], "epic", row["account_id"], display_name=row["display_name"], checked_at=checked,
        )
        user_modes = modes[row["user_id"]]
        ranks = {
            mode["mode"]: {
                "rank": mode["rank_name"], "track_id": mode["rank_track_id"],
                "source": mode["rank_source"], "checked_at": iso(mode["rank_checked_at"]),
            }
            for mode in user_modes if any(
                mode[key] is not None for key in ("rank_name", "rank_track_id", "rank_source", "rank_checked_at")
            )
        }
        details = {
            target: {mode["mode"]: mode[source] for mode in user_modes if mode[source] is not None}
            for source, target in (
                ("lifetime_minutes", "lifetime_modes"), ("recent_minutes", "recent_minutes"),
                ("recent_matches", "recent_matches"),
            )
        }
        favorite = row["favorite_mode"]
        mode = favorite_rank_mode(favorite, {key: value["rank"] for key, value in ranks.items()})
        rank = ranks.get(mode, {})
        details.update(
            ranks=ranks, favorite_mode=favorite, recent_checked_at=iso(row["recent_checked_at"]),
            playtime_source=row["playtime_source"],
        )
        add_stats(
            account, FORTNITE_GAME_ID, checked_at=checked, rank=rank.get("rank"), minutes=row["total_minutes"],
            source=rank.get("source") or row["playtime_source"],
            scope="favorite_mode" if favorite else "available_history", details=details,
        )

    riot = sorted(rows("riot_profiles"), key=lambda row: (row["user_id"], row["checked_at"], row["game"]))
    for user_id, profiles in groupby(riot, key=lambda row: row["user_id"]):
        profiles = list(profiles)
        latest = profiles[-1]
        identity = f"{latest['game_name']}#{latest['tag_line']}"
        active, previous = [], []
        for row in profiles:
            riot_id = f"{row['game_name']}#{row['tag_line']}"
            if riot_id.casefold() == identity.casefold() and row["game"] in RIOT_IDS:
                active.append(row)
            else:
                previous.append(dict(row, checked_at=iso(row["checked_at"])))
        account = add_account(
            user_id, "riot", identity.casefold(), display_name=identity, checked_at=latest["checked_at"],
            details={"previous_profiles": previous} if previous else {},
        )
        for row in active:
            add_stats(
                account, RIOT_IDS[row["game"]], checked_at=row["checked_at"], rank=row["rank"],
                source=row["source"], scope=row["match_scope"], matches=row["match_count"],
                details={"region": row["region"]},
            )

    for table in legacy:
        op.drop_table(table)
    for column in STEAM_COLUMNS:
        op.drop_column("users", column)
