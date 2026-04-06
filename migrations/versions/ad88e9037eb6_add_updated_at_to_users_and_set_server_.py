"""add updated_at to users and set server_default on created_at

Revision ID: ad88e9037eb6
Revises: 874259efad71
Create Date: 2026-04-06

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel  # noqa: F401

revision: str = "ad88e9037eb6"
down_revision: Union[str, Sequence[str], None] = "874259efad71"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add updated_at column to users table and set server_default on created_at."""
    op.add_column(
        "users",
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )
    # Ensure created_at has a DB-level default for rows inserted outside the ORM.
    op.alter_column(
        "users",
        "created_at",
        server_default=sa.func.now(),
    )


def downgrade() -> None:
    """Remove updated_at column from users table."""
    op.alter_column("users", "created_at", server_default=None)
    op.drop_column("users", "updated_at")
