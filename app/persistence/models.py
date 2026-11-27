from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True)
    username: Mapped[str | None] = mapped_column(String(64))
    share_username: Mapped[bool] = mapped_column(Boolean, default=False)
    looking_for_game: Mapped[int | None] = mapped_column(BigInteger)
    looking_until: Mapped[datetime | None] = mapped_column(DateTime)
    notify_new_profiles: Mapped[bool] = mapped_column(Boolean, default=False)
    name: Mapped[str] = mapped_column(String(40))
    age: Mapped[int] = mapped_column(Integer)
    gender: Mapped[str] = mapped_column(String(16), default="other")
    looking_for: Mapped[str] = mapped_column(String(16), default="any")
    age_min: Mapped[int] = mapped_column(default=18)
    age_max: Mapped[int] = mapped_column(default=99)
    purpose: Mapped[str] = mapped_column(String(16), default="both")
    timezone: Mapped[str] = mapped_column(String(64), default="Europe/Moscow")
    start_hour: Mapped[int] = mapped_column(default=19)
    end_hour: Mapped[int] = mapped_column(default=23)
    schedule_set: Mapped[bool] = mapped_column(Boolean, default=True)
    bio: Mapped[str] = mapped_column(Text, default="")
    photo_file_id: Mapped[str | None] = mapped_column(String(1024))
    use_default_avatar: Mapped[bool] = mapped_column(Boolean, default=False)
    test_photo_override: Mapped[bool] = mapped_column(Boolean, default=False)
    language: Mapped[str] = mapped_column(String(16), default="ru")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    banned: Mapped[bool] = mapped_column(Boolean, default=False)
    is_test: Mapped[bool] = mapped_column(Boolean, default=False)
    consent_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_active: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    games: Mapped[list["UserGame"]] = relationship(cascade="all, delete-orphan", lazy="selectin")
    accounts: Mapped[list["GameAccount"]] = relationship(cascade="all, delete-orphan", lazy="selectin")

    def game_account(self, platform: str):
        return next((account for account in self.accounts if account.platform == platform), None)

    @property
    def steam_id(self):
        account = self.game_account("steam")
        return account.external_account_id if account else None

    @property
    def steam_checked_at(self):
        account = self.game_account("steam")
        return account.checked_at if account else None

    @property
    def steam_import_status(self):
        account = self.game_account("steam")
        return account.status if account else "not_loaded"


class Game(Base):
    __tablename__ = "games"
    app_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    name: Mapped[str] = mapped_column(String(160))
    slug: Mapped[str] = mapped_column(String(96), unique=True)
    genres: Mapped[list] = mapped_column(JSON, default=list)
    aliases: Mapped[list] = mapped_column(JSON, default=list)


class UserGame(Base):
    __tablename__ = "user_games"
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    app_id: Mapped[int] = mapped_column("game_id", ForeignKey("games.app_id"), primary_key=True)
    minutes: Mapped[int] = mapped_column(BigInteger, default=0)
    recent_minutes: Mapped[int] = mapped_column(BigInteger, default=0)
    dota_match_minutes: Mapped[int] = mapped_column(BigInteger, default=0)
    selected: Mapped[bool] = mapped_column(Boolean, default=False)
    imported: Mapped[bool] = mapped_column(Boolean, default=False)
    game: Mapped[Game] = relationship(lazy="joined")

    @property
    def effective_minutes(self) -> int:
        return self.minutes or self.dota_match_minutes or 0


class GameAccount(Base):
    __tablename__ = "game_accounts"
    __table_args__ = (UniqueConstraint("user_id", "platform", name="uq_game_accounts_user_platform"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    platform: Mapped[str] = mapped_column(String(16))
    external_account_id: Mapped[str] = mapped_column(String(96))
    display_name: Mapped[str | None] = mapped_column(String(80))
    checked_at: Mapped[datetime | None] = mapped_column(DateTime)
    status: Mapped[str] = mapped_column(String(24), default="linked")
    details: Mapped[dict] = mapped_column(JSONB, default=dict)


class GameStats(Base):
    __tablename__ = "game_stats"
    __table_args__ = (
        UniqueConstraint("account_id", "game_id", name="uq_game_stats_account_game"),
        CheckConstraint("matches_count >= 0", name="ck_game_stats_matches_count"),
        CheckConstraint("playtime_minutes >= 0", name="ck_game_stats_playtime_minutes"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("game_accounts.id", ondelete="CASCADE"))
    game_id: Mapped[int] = mapped_column(ForeignKey("games.app_id"), index=True)
    rank: Mapped[str | None] = mapped_column(String(64))
    matches_count: Mapped[int | None] = mapped_column(BigInteger)
    playtime_minutes: Mapped[int | None] = mapped_column(BigInteger)
    source: Mapped[str | None] = mapped_column(String(24))
    scope: Mapped[str | None] = mapped_column(String(32))
    checked_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    details: Mapped[dict] = mapped_column(JSONB, default=dict)
    account: Mapped[GameAccount] = relationship(lazy="joined")


class Reaction(Base):
    __tablename__ = "reactions"
    actor_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    target_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    kind: Mapped[str] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Match(Base):
    __tablename__ = "matches"
    id: Mapped[int] = mapped_column(primary_key=True)
    low_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    high_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    __table_args__ = (UniqueConstraint("low_id", "high_id"),)


class Block(Base):
    __tablename__ = "blocks"
    actor_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    target_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)


class Report(Base):
    __tablename__ = "reports"
    id: Mapped[int] = mapped_column(primary_key=True)
    actor_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    target_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    reason: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    resolved: Mapped[bool] = mapped_column(Boolean, default=False)


class Event(Base):
    __tablename__ = "events"
    id: Mapped[int] = mapped_column(primary_key=True)
    actor_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    target_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String(32))
    details: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class FSMRecord(Base):
    __tablename__ = "fsm_records"
    key: Mapped[str] = mapped_column(String(255), primary_key=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, index=True)
    state: Mapped[str | None] = mapped_column(String(128))
    data: Mapped[dict] = mapped_column(JSON, default=dict)


class DeveloperAudit(Base):
    __tablename__ = "developer_audit"
    id: Mapped[int] = mapped_column(primary_key=True)
    actor_telegram_id: Mapped[int] = mapped_column(BigInteger, index=True)
    target_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    action: Mapped[str] = mapped_column(String(40))
    details: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
