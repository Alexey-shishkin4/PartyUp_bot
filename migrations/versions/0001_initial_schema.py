"""Create the PartyUp schema.

Revision ID: 0001
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "fsm_records",
        sa.Column("key", sa.String(length=255), nullable=False),
        sa.Column("telegram_id", sa.BigInteger(), nullable=False),
        sa.Column("state", sa.String(length=128), nullable=True),
        sa.Column("data", sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint("key"),
    )
    op.create_index(op.f("ix_fsm_records_telegram_id"), "fsm_records", ["telegram_id"], unique=False)
    op.create_table(
        "games",
        sa.Column("app_id", sa.BigInteger(), autoincrement=False, nullable=False),
        sa.Column("name", sa.String(length=160), nullable=False),
        sa.Column("slug", sa.String(length=96), nullable=False),
        sa.Column("genres", sa.JSON(), nullable=False),
        sa.Column("aliases", sa.JSON(), nullable=False),
        sa.Column("coop", sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint("app_id"),
        sa.UniqueConstraint("slug"),
    )
    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("telegram_id", sa.BigInteger(), nullable=False),
        sa.Column("username", sa.String(length=64), nullable=True),
        sa.Column("share_username", sa.Boolean(), nullable=False),
        sa.Column("looking_for_game", sa.BigInteger(), nullable=True),
        sa.Column("looking_until", sa.DateTime(), nullable=True),
        sa.Column("notify_new_profiles", sa.Boolean(), nullable=False),
        sa.Column("name", sa.String(length=40), nullable=False),
        sa.Column("age", sa.Integer(), nullable=False),
        sa.Column("gender", sa.String(length=16), nullable=False),
        sa.Column("looking_for", sa.String(length=16), nullable=False),
        sa.Column("age_min", sa.Integer(), nullable=False),
        sa.Column("age_max", sa.Integer(), nullable=False),
        sa.Column("purpose", sa.String(length=16), nullable=False),
        sa.Column("timezone", sa.String(length=64), nullable=False),
        sa.Column("start_hour", sa.Integer(), nullable=False),
        sa.Column("end_hour", sa.Integer(), nullable=False),
        sa.Column("schedule_set", sa.Boolean(), nullable=False),
        sa.Column("bio", sa.Text(), nullable=False),
        sa.Column("photo_file_id", sa.String(length=1024), nullable=True),
        sa.Column("use_default_avatar", sa.Boolean(), nullable=False),
        sa.Column("test_photo_override", sa.Boolean(), nullable=False),
        sa.Column("language", sa.String(length=16), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("banned", sa.Boolean(), nullable=False),
        sa.Column("is_test", sa.Boolean(), nullable=False),
        sa.Column("consent_at", sa.DateTime(), nullable=False),
        sa.Column("last_active", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("telegram_id"),
    )
    op.create_table(
        "blocks",
        sa.Column("actor_id", sa.Integer(), nullable=False),
        sa.Column("target_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["actor_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["target_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("actor_id", "target_id"),
    )
    op.create_table(
        "developer_audit",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("actor_telegram_id", sa.BigInteger(), nullable=False),
        sa.Column("target_user_id", sa.Integer(), nullable=True),
        sa.Column("action", sa.String(length=40), nullable=False),
        sa.Column("details", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["target_user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_developer_audit_actor_telegram_id"), "developer_audit", ["actor_telegram_id"], unique=False
    )
    op.create_index(
        op.f("ix_developer_audit_target_user_id"), "developer_audit", ["target_user_id"], unique=False
    )
    op.create_table(
        "events",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("actor_id", sa.Integer(), nullable=False),
        sa.Column("target_id", sa.Integer(), nullable=True),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("details", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["actor_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["target_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_events_actor_id"), "events", ["actor_id"], unique=False)
    op.create_table(
        "game_accounts",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("platform", sa.String(length=16), nullable=False),
        sa.Column("external_account_id", sa.String(length=96), nullable=False),
        sa.Column("display_name", sa.String(length=80), nullable=True),
        sa.Column("checked_at", sa.DateTime(), nullable=True),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("details", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "platform", name="uq_game_accounts_user_platform"),
    )
    op.create_table(
        "matches",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("low_id", sa.Integer(), nullable=False),
        sa.Column("high_id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["high_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["low_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("low_id", "high_id"),
    )
    op.create_table(
        "reactions",
        sa.Column("actor_id", sa.Integer(), nullable=False),
        sa.Column("target_id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["actor_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["target_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("actor_id", "target_id"),
    )
    op.create_table(
        "reports",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("actor_id", sa.Integer(), nullable=False),
        sa.Column("target_id", sa.Integer(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("resolved", sa.Boolean(), nullable=False),
        sa.ForeignKeyConstraint(["actor_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["target_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "user_games",
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("game_id", sa.BigInteger(), nullable=False),
        sa.Column("minutes", sa.BigInteger(), nullable=False),
        sa.Column("recent_minutes", sa.BigInteger(), nullable=False),
        sa.Column("dota_match_minutes", sa.BigInteger(), nullable=False),
        sa.Column("selected", sa.Boolean(), nullable=False),
        sa.Column("imported", sa.Boolean(), nullable=False),
        sa.ForeignKeyConstraint(
            ["game_id"],
            ["games.app_id"],
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("user_id", "game_id"),
    )
    op.create_table(
        "game_stats",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("game_id", sa.BigInteger(), nullable=False),
        sa.Column("rank", sa.String(length=64), nullable=True),
        sa.Column("matches_count", sa.BigInteger(), nullable=True),
        sa.Column("playtime_minutes", sa.BigInteger(), nullable=True),
        sa.Column("source", sa.String(length=24), nullable=True),
        sa.Column("scope", sa.String(length=32), nullable=True),
        sa.Column("checked_at", sa.DateTime(), nullable=False),
        sa.Column("details", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.CheckConstraint("matches_count >= 0", name="ck_game_stats_matches_count"),
        sa.CheckConstraint("playtime_minutes >= 0", name="ck_game_stats_playtime_minutes"),
        sa.ForeignKeyConstraint(["account_id"], ["game_accounts.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["game_id"],
            ["games.app_id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("account_id", "game_id", name="uq_game_stats_account_game"),
    )
    op.create_index(op.f("ix_game_stats_game_id"), "game_stats", ["game_id"], unique=False)


def downgrade():
    op.drop_index(op.f("ix_game_stats_game_id"), table_name="game_stats")
    op.drop_table("game_stats")
    op.drop_table("user_games")
    op.drop_table("reports")
    op.drop_table("reactions")
    op.drop_table("matches")
    op.drop_table("game_accounts")
    op.drop_index(op.f("ix_events_actor_id"), table_name="events")
    op.drop_table("events")
    op.drop_index(op.f("ix_developer_audit_target_user_id"), table_name="developer_audit")
    op.drop_index(op.f("ix_developer_audit_actor_telegram_id"), table_name="developer_audit")
    op.drop_table("developer_audit")
    op.drop_table("blocks")
    op.drop_table("users")
    op.drop_table("games")
    op.drop_index(op.f("ix_fsm_records_telegram_id"), table_name="fsm_records")
    op.drop_table("fsm_records")
