import asyncio
from datetime import datetime, timedelta

from app.domain.errors import DomainError
from app.domain.faceit import FaceitLookup
from app.domain.game_links_draft import CheckedSnapshot, RiotLinkDraft
from app.domain.pubg import parse_account
from app.domain.riot import VALORANT_ACT, RiotGame, RiotId
from app.domain.steam import CS2_APP_ID, DEADLOCK_APP_ID
from app.persistence.models import utcnow

from .game_labels import RIOT_GAME_NAMES

REFRESH_INTERVAL = timedelta(minutes=15)
PUBG_PLAYTIME_INTERVAL = timedelta(hours=6)


class GameAccounts:
    def __init__(self, services, **fetchers):
        self.services = services
        self.fetchers = {name: fetch for name, fetch in fetchers.items() if fetch is not None}

    @property
    def draft_providers(self):
        return tuple(name for name in ("riot", "fortnite", "pubg", "faceit", "deadlock") if name in self.fetchers)

    @staticmethod
    def played(user, app_id):
        return any(
            row.app_id == app_id and row.imported and (row.minutes > 0 or row.recent_minutes > 0)
            for row in user.games
        )

    def connected(self, user):
        linked = {}
        for name in self.fetchers:
            if name == "riot":
                entries = self.riot_entries(user.id)
                row = max((row for row in entries.values() if row), key=lambda row: row.checked_at, default=None)
            elif name == "deadlock":
                row = self.services.deadlock.get(user.id, user.steam_id) if user.steam_id else None
            else:
                row = getattr(self.services, name).get(user.id)
            if row:
                linked[name] = row
        return linked

    def riot_entries(self, user_id):
        return {game: self.services.riot.get(user_id, game) for game in RiotGame}

    @staticmethod
    def fresh(checked_at, *, interval=REFRESH_INTERVAL):
        return checked_at is not None and utcnow() - checked_at < interval

    async def sync_steam(self, user, *, skip=()):
        jobs = {}
        if "faceit" in self.fetchers and "faceit" not in skip and self.played(user, CS2_APP_ID):
            row = self.services.faceit.get(user.id)
            if not row or row.account.details.get("steam_id") == user.steam_id:
                jobs["FACEIT"] = self.faceit(user, FaceitLookup("steam_id", user.steam_id), from_steam=True)
        if "deadlock" in self.fetchers and "deadlock" not in skip and self.played(user, DEADLOCK_APP_ID):
            jobs["Deadlock"] = self.deadlock(user)
        return await self.steam_results(jobs)

    async def sync_draft_steam(self, draft, profile, *, check_deadlock=False):
        played = {
            row["appid"] for row in profile.games
            if row.get("playtime_forever", 0) > 0 or row.get("playtime_2weeks", 0) > 0
        }
        jobs = {}
        if "faceit" in self.fetchers and not draft.faceit and CS2_APP_ID in played:
            jobs["FACEIT"] = self.link_draft(draft, "faceit", FaceitLookup("steam_id", profile.steam_id))
        if "deadlock" in self.fetchers and not draft.deadlock and (check_deadlock or DEADLOCK_APP_ID in played):
            jobs["Deadlock"] = self.link_draft(draft, "deadlock", profile.steam_id)
        return await self.steam_results(jobs)

    @staticmethod
    async def steam_results(jobs):
        results = await asyncio.gather(*jobs.values(), return_exceptions=True)
        unavailable = []
        for game, result in zip(jobs, results):
            if isinstance(result, DomainError):
                unavailable.append(game)
            elif isinstance(result, BaseException):
                raise result
        return unavailable

    @staticmethod
    def parse_identity(name, value):
        if name == "riot":
            return RiotId.parse(value)
        if name == "pubg":
            return parse_account(value)
        if name == "faceit":
            return FaceitLookup.parse(value)
        nickname = value.strip()
        if not 2 <= len(nickname) <= 32 or not nickname.isprintable() or any(c in nickname for c in "/\\"):
            raise DomainError("Укажи ник Epic Games без ссылки (от 2 до 32 символов).")
        return nickname

    async def link(self, user, name, identity):
        if name == "riot":
            return await self.riot(user, identity)
        if name == "pubg":
            await self.pubg(user, *identity)
        elif name == "faceit":
            await self.faceit(user, identity)
        elif name == "fortnite":
            await self.fortnite(user, identity)
        return []

    async def link_draft(self, draft, name, identity):
        if name == "riot":
            snapshots, errors = await self.fetch_riot(identity, dict.fromkeys(RiotGame))
            draft.riot = RiotLinkDraft(identity, snapshots, utcnow())
            return [RIOT_GAME_NAMES[game] for game in errors]
        if name == "fortnite":
            draft.fortnite = await self.fetchers[name](identity, account_id=None)
        elif name == "pubg":
            stats = await self.fetchers[name](*identity, account_id=None, need_playtime=True, need_rank=True)
            draft.pubg = CheckedSnapshot(stats, utcnow())
        else:
            stats = await self.fetchers[name](identity)
            setattr(draft, name, CheckedSnapshot(stats, utcnow()))
        return []

    async def refresh(self, user, name, row):
        if name == "riot":
            return await self.riot(user, RiotId.parse(row.account.display_name))
        if name == "pubg":
            await self.pubg(user, row.account.details["shard"], row.account.display_name)
        elif name == "fortnite":
            await self.fortnite(user, row.account.display_name, row.account.external_account_id)
        elif name == "faceit":
            await self.faceit(user, FaceitLookup(
                "player_id", row.account.external_account_id, row.account.details.get("steam_id"),
            ))
        elif name == "deadlock":
            await self.deadlock(user)
        return []

    def disconnect(self, user, name):
        getattr(self.services, name).disconnect(user.id)

    async def riot(self, user, account):
        existing = self.riot_entries(user.id)
        regions = {}
        for game, row in existing.items():
            same = row and row.account.external_account_id == "#".join(account.normalized)
            if same and self.fresh(row.checked_at) and row.scope != VALORANT_ACT:
                continue
            regions[game] = row.details.get("region") if same else None
        snapshots, errors = await self.fetch_riot(account, regions)
        if snapshots:
            expected = {game: row.checked_at if row else None for game, row in existing.items()}
            self.services.riot.save_account(user.id, account, snapshots, expected)
        return [RIOT_GAME_NAMES[game] for game in errors]

    async def fetch_riot(self, account, regions):
        jobs = {game: self.fetchers["riot"](game, region, account) for game, region in regions.items()}
        results = await asyncio.gather(*jobs.values(), return_exceptions=True)
        snapshots, errors = {}, {}
        for game, result in zip(jobs, results):
            if isinstance(result, DomainError):
                errors[game] = result
            elif isinstance(result, BaseException):
                raise result
            elif result.riot_id.normalized != account.normalized:
                errors[game] = DomainError("Источник вернул статистику для другого Riot ID.")
            else:
                snapshots[game] = result
        if len(errors) == len(RiotGame):
            raise DomainError("\n".join(f"{RIOT_GAME_NAMES[game]} · {error}" for game, error in errors.items()))
        return snapshots, errors

    async def pubg(self, user, platform, identity):
        row = self.services.pubg.get(user.id)
        same = row and row.account.details["shard"] == platform and identity.casefold() in {
            row.account.display_name.casefold(), row.account.external_account_id,
        }
        playtime_at = row.details.get("playtime_at") if same else None
        rank_at = row.details.get("rank_at") if same else None
        need_playtime = not self.fresh(
            datetime.fromisoformat(playtime_at) if playtime_at else None, interval=PUBG_PLAYTIME_INTERVAL,
        )
        need_rank = not self.fresh(datetime.fromisoformat(rank_at) if rank_at else None)
        if need_playtime or need_rank:
            stats = await self.fetchers["pubg"](
                platform, row.account.display_name if same else identity,
                account_id=row.account.external_account_id if same else None,
                need_playtime=need_playtime, need_rank=need_rank,
            )
            self.services.pubg.save(user.id, stats, expected_checked_at=row.checked_at if row else None)

    async def fortnite(self, user, nickname, account_id=None):
        row = self.services.fortnite.get(user.id)
        same = row and (
            row.account.external_account_id == account_id or row.account.display_name.casefold() == nickname.casefold()
        )
        if same and self.fresh(row.checked_at):
            return
        previous_id = row.account.external_account_id if same else None
        snapshot = await self.fetchers["fortnite"](nickname, account_id=previous_id)
        if same and snapshot.playtime is None and snapshot.activity is None and snapshot.ranks is None:
            raise DomainError("Fortnite временно не отдал статистику. Сохранённые данные доступны в анкете.")
        self.services.fortnite.save(user.id, snapshot, expected_account_id=previous_id)

    async def faceit(self, user, lookup, *, from_steam=False):
        row = self.services.faceit.get(user.id)
        same = row and (
            (lookup.kind == "steam_id" and lookup.value == row.account.details.get("steam_id"))
            or (lookup.kind == "player_id" and lookup.value == row.account.external_account_id)
            or (lookup.kind == "nickname" and lookup.value.casefold() == row.account.display_name.casefold())
        )
        if not same or not self.fresh(row.checked_at):
            stats = await self.fetchers["faceit"](lookup)
            self.services.faceit.save(
                user.id, stats, expected_checked_at=row.checked_at if row else None,
                expected_steam_id=user.steam_id if from_steam else None,
            )

    async def deadlock(self, user):
        if not user.steam_id:
            raise DomainError("Подключи Steam, чтобы добавить Deadlock.")
        row = self.services.deadlock.get(user.id, user.steam_id)
        interval = timedelta(minutes=1) if row and row.matches_count is None else REFRESH_INTERVAL
        if not row or not self.fresh(row.checked_at, interval=interval):
            stats = await self.fetchers["deadlock"](user.steam_id)
            self.services.deadlock.save(user.id, user.steam_id, stats, expected_checked_at=row.checked_at if row else None)
