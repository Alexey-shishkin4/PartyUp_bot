from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow():
    return datetime.utcnow()


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True)
    username: Mapped[str | None] = mapped_column(String(64))
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
    bio: Mapped[str] = mapped_column(Text, default="")
    photo_file_id: Mapped[str | None] = mapped_column(String(1024))
    test_photo_override: Mapped[bool] = mapped_column(Boolean, default=False)
    language: Mapped[str] = mapped_column(String(16), default="ru")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    banned: Mapped[bool] = mapped_column(Boolean, default=False)
    is_demo: Mapped[bool] = mapped_column(Boolean, default=False)
    is_test: Mapped[bool] = mapped_column(Boolean, default=False)
    steam_id: Mapped[str | None] = mapped_column(String(20))
    steam_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    steam_checked_at: Mapped[datetime | None] = mapped_column(DateTime)
    steam_import_status: Mapped[str] = mapped_column(String(24), default="not_loaded")
    consent_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_active: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    games: Mapped[list["UserGame"]] = relationship(cascade="all, delete-orphan", lazy="selectin")


class Game(Base):
    __tablename__ = "games"
    app_id: Mapped[int] = mapped_column(primary_key=True, autoincrement=False)
    name: Mapped[str] = mapped_column(String(160))
    genres: Mapped[list] = mapped_column(JSON, default=list)
    aliases: Mapped[list] = mapped_column(JSON, default=list)
    coop: Mapped[bool] = mapped_column(Boolean, default=False)


class UserGame(Base):
    __tablename__ = "user_games"
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    app_id: Mapped[int] = mapped_column(ForeignKey("games.app_id"), primary_key=True)
    minutes: Mapped[int] = mapped_column(default=0)
    recent_minutes: Mapped[int] = mapped_column(default=0)
    selected: Mapped[bool] = mapped_column(Boolean, default=False)
    imported: Mapped[bool] = mapped_column(Boolean, default=False)
    game: Mapped[Game] = relationship(lazy="joined")


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


class LoginTicket(Base):
    __tablename__ = "login_tickets"
    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    browser_hash: Mapped[str | None] = mapped_column(String(64))
    expires_at: Mapped[datetime] = mapped_column(DateTime)


class FSMRecord(Base):
    __tablename__ = "fsm_records"
    key: Mapped[str] = mapped_column(String(255), primary_key=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, index=True)
    state: Mapped[str | None] = mapped_column(String(128))
    data: Mapped[dict] = mapped_column(JSON, default=dict)


class DeveloperAudit(Base):
    __tablename__ = "developer_audit"
    id: Mapped[int] = mapped_column(primary_key=True)
    actor_telegram_id: Mapped[int] = mapped_column(BigInteger,index=True)
    target_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id",ondelete="CASCADE"),index=True)
    action: Mapped[str] = mapped_column(String(40))
    details: Mapped[dict] = mapped_column(JSON,default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime,default=utcnow)
