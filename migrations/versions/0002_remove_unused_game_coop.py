"""Remove the unused coop flag from games.

Revision ID: 0002
Revises: 0001
"""

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade():
    op.drop_column("games", "coop")


def downgrade():
    op.add_column("games", sa.Column("coop", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.alter_column("games", "coop", server_default=None)
