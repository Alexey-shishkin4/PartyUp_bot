from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
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
    steam_id: Mapped[str | None] = mapped_column(String(20))
    steam_checked_at: Mapped[datetime | None] = mapped_column(DateTime)
    steam_import_status: Mapped[str] = mapped_column(String(24), default="not_loaded")
    dota_rank_tier: Mapped[int | None]
    consent_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_active: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    games: Mapped[list["UserGame"]] = relationship(cascade="all, delete-orphan", lazy="selectin")


class Game(Base):
    __tablename__ = "games"
    app_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    name: Mapped[str] = mapped_column(String(160))
    genres: Mapped[list] = mapped_column(JSON, default=list)
    aliases: Mapped[list] = mapped_column(JSON, default=list)
    coop: Mapped[bool] = mapped_column(Boolean, default=False)


class UserGame(Base):
    __tablename__ = "user_games"
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    app_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("games.app_id"), primary_key=True)
    minutes: Mapped[int] = mapped_column(default=0)
    recent_minutes: Mapped[int] = mapped_column(default=0)
    dota_match_minutes: Mapped[int] = mapped_column(default=0)
    selected: Mapped[bool] = mapped_column(Boolean, default=False)
    imported: Mapped[bool] = mapped_column(Boolean, default=False)
    game: Mapped[Game] = relationship(lazy="joined")

    @property
    def effective_minutes(self) -> int:
        return self.minutes or self.dota_match_minutes or 0


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


class FortniteAccount(Base):
    __tablename__ = "fortnite_accounts"
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    account_id: Mapped[str] = mapped_column(String(32), index=True)
    display_name: Mapped[str] = mapped_column(String(64))
    total_minutes: Mapped[int | None]
    playtime_source: Mapped[str | None] = mapped_column(String(24))
    favorite_mode: Mapped[str | None] = mapped_column(String(64))
    recent_checked_at: Mapped[datetime | None]
    checked_at: Mapped[datetime] = mapped_column(DateTime)
    modes: Mapped[list["FortniteModeStats"]] = relationship(
        cascade="all, delete-orphan", lazy="selectin", order_by="FortniteModeStats.mode"
    )


class FortniteModeStats(Base):
    __tablename__ = "fortnite_mode_stats"
    user_id: Mapped[int] = mapped_column(
        ForeignKey("fortnite_accounts.user_id", ondelete="CASCADE"), primary_key=True
    )
    mode: Mapped[str] = mapped_column(String(64), primary_key=True)
    lifetime_minutes: Mapped[int | None]
    recent_minutes: Mapped[int | None]
    recent_matches: Mapped[int | None]
    rank_name: Mapped[str | None] = mapped_column(String(64))
    rank_track_id: Mapped[str | None] = mapped_column(String(64))
    rank_source: Mapped[str | None] = mapped_column(String(24))
    rank_checked_at: Mapped[datetime | None]


class RiotProfile(Base):
    __tablename__ = "riot_profiles"
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    game: Mapped[str] = mapped_column(String(16), primary_key=True)
    region: Mapped[str] = mapped_column(String(8))
    game_name: Mapped[str] = mapped_column(String(16))
    tag_line: Mapped[str] = mapped_column(String(5))
    rank: Mapped[str | None] = mapped_column(String(48))
    match_count: Mapped[int | None]
    match_scope: Mapped[str] = mapped_column(String(24))
    source: Mapped[str] = mapped_column(String(16))
    checked_at: Mapped[datetime] = mapped_column(DateTime)


class PubgProfile(Base):
    __tablename__ = "pubg_profiles"
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    account_id: Mapped[str] = mapped_column(String(40))
    nickname: Mapped[str] = mapped_column(String(32))
    platform: Mapped[str] = mapped_column(String(8))
    mode_seconds: Mapped[dict | None] = mapped_column(JSON)
    ranks: Mapped[dict | None] = mapped_column(JSON)
    playtime_source: Mapped[str | None] = mapped_column(String(16))
    rank_source: Mapped[str | None] = mapped_column(String(16))
    season_id: Mapped[str | None] = mapped_column(String(96))
    playtime_at: Mapped[datetime | None] = mapped_column(DateTime)
    rank_at: Mapped[datetime | None] = mapped_column(DateTime)
class FaceitProfile(Base):
    __tablename__ = "faceit_profiles"
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    player_id: Mapped[str] = mapped_column(String(36))
    nickname: Mapped[str] = mapped_column(String(32))
    skill_level: Mapped[int | None]
    elo: Mapped[int | None]
    match_count: Mapped[int | None]
    wins: Mapped[int | None]
    win_rate: Mapped[float | None]
    checked_at: Mapped[datetime] = mapped_column(DateTime)
    steam_id: Mapped[str | None] = mapped_column(String(20))
    source: Mapped[str] = mapped_column(String(16), server_default="faceit")
class DeadlockProfile(Base):
    __tablename__ = "deadlock_profiles"
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    steam_id: Mapped[str] = mapped_column(String(20))
    rank_badge: Mapped[int]
    ranked_at: Mapped[datetime | None] = mapped_column(DateTime)
    match_count: Mapped[int | None]
    match_seconds: Mapped[int | None] = mapped_column(BigInteger)
    checked_at: Mapped[datetime] = mapped_column(DateTime)
