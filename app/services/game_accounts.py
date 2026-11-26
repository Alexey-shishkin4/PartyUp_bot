import asyncio
from datetime import timedelta

from app.domain.errors import DomainError
from app.domain.faceit import FaceitLookup
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
    def fresh(row, *, field="checked_at", interval=REFRESH_INTERVAL):
        checked_at = getattr(row, field, None)
        return checked_at is not None and utcnow() - checked_at < interval

    async def sync_steam(self, user):
        jobs = {}
        if "faceit" in self.fetchers and self.played(user, CS2_APP_ID):
            row = self.services.faceit.get(user.id)
            if not row or row.steam_id == user.steam_id:
                jobs["FACEIT"] = self.faceit(user, FaceitLookup("steam_id", user.steam_id), from_steam=True)
        if "deadlock" in self.fetchers and self.played(user, DEADLOCK_APP_ID):
            jobs["Deadlock"] = self.deadlock(user)
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

    async def refresh(self, user, name, row):
        if name == "riot":
            existing = self.riot_entries(user.id)
            account = RiotId(row.game_name, row.tag_line)
            if any(
                linked and RiotId(linked.game_name, linked.tag_line).normalized != account.normalized
                for linked in existing.values()
            ):
                raise DomainError("Для игр сохранены разные Riot ID. В «Сменить аккаунт» укажи один ID для обеих игр.")
            return await self.riot(user, account, existing=existing)
        if name == "pubg":
            await self.pubg(user, row.platform, row.nickname)
        elif name == "fortnite":
            await self.fortnite(user, row.display_name, row.account_id)
        elif name == "faceit":
            await self.faceit(user, FaceitLookup("player_id", row.player_id, row.steam_id))
        elif name == "deadlock":
            await self.deadlock(user)
        return []

    def disconnect(self, user, name):
        getattr(self.services, name).disconnect(user.id)

    async def riot(self, user, account, *, existing=None):
        if existing is None:
            existing = self.riot_entries(user.id)
        jobs = {}
        for game, row in existing.items():
            same = row and RiotId(row.game_name, row.tag_line).normalized == account.normalized
            if same and self.fresh(row) and row.match_scope != VALORANT_ACT:
                continue
            jobs[game] = self.fetchers["riot"](game, row.region if same else None, account)
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
        if snapshots:
            expected = {game: row.checked_at if row else None for game, row in existing.items()}
            self.services.riot.save_account(user.id, account, snapshots, expected)
        elif errors and len(errors) == len(RiotGame):
            raise DomainError("\n".join(f"{RIOT_GAME_NAMES[game]} · {error}" for game, error in errors.items()))
        return [RIOT_GAME_NAMES[game] for game in errors]

    async def pubg(self, user, platform, identity):
        row = self.services.pubg.get(user.id)
        same = row and row.platform == platform and identity.casefold() in {row.nickname.casefold(), row.account_id}
        need_playtime = not same or not self.fresh(row, field="playtime_at", interval=PUBG_PLAYTIME_INTERVAL)
        need_rank = not same or not self.fresh(row, field="rank_at")
        if need_playtime or need_rank:
            stats = await self.fetchers["pubg"](
                platform, row.nickname if same else identity, account_id=row.account_id if same else None,
                need_playtime=need_playtime, need_rank=need_rank,
            )
            self.services.pubg.save(user.id, stats, expected_checked_at=row.checked_at if row else None)

    async def fortnite(self, user, nickname, account_id=None):
        row = self.services.fortnite.get(user.id)
        same = row and (row.account_id == account_id or row.display_name.casefold() == nickname.casefold())
        if same and self.fresh(row):
            return
        snapshot = await self.fetchers["fortnite"](nickname, account_id=row.account_id if same else None)
        if same and snapshot.playtime is None and snapshot.activity is None and snapshot.ranks is None:
            raise DomainError("Fortnite временно не отдал статистику. Сохранённые данные доступны в анкете.")
        self.services.fortnite.save(user.id, snapshot, expected_account_id=row.account_id if same else None)

    async def faceit(self, user, lookup, *, from_steam=False):
        row = self.services.faceit.get(user.id)
        same = row and (
            (lookup.kind == "steam_id" and lookup.value == row.steam_id)
            or (lookup.kind == "player_id" and lookup.value == row.player_id)
            or (lookup.kind == "nickname" and lookup.value.casefold() == row.nickname.casefold())
        )
        if not same or not self.fresh(row):
            stats = await self.fetchers["faceit"](lookup)
            self.services.faceit.save(
                user.id, stats, expected_checked_at=row.checked_at if row else None,
                expected_steam_id=user.steam_id if from_steam else None,
            )

    async def deadlock(self, user):
        if not user.steam_id:
            raise DomainError("Подключи Steam, чтобы добавить Deadlock.")
        row = self.services.deadlock.get(user.id, user.steam_id)
        interval = timedelta(minutes=1) if row and row.match_count is None else REFRESH_INTERVAL
        if not self.fresh(row, interval=interval):
            stats = await self.fetchers["deadlock"](user.steam_id)
            self.services.deadlock.save(user.id, user.steam_id, stats, expected_checked_at=row.checked_at if row else None)
