"""add set_updated_at trigger function

Revision ID: 874259efad71
Revises:
Create Date: 2026-04-06

Creates a reusable PostgreSQL trigger function ``set_updated_at()`` and a
helper function ``apply_updated_at_trigger(tbl)`` that attaches the trigger
to any table with an ``updated_at`` column.

The trigger sets ``updated_at = now()`` on every UPDATE, catching direct SQL
updates that bypass the ORM layer.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel  # noqa: F401

revision: str = "874259efad71"
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Tables that currently have an updated_at column.
# Extend this list as new models are added.
TABLES_WITH_UPDATED_AT: list[str] = [
    "users",
]


def upgrade() -> None:
    """Create the trigger function and attach it to all relevant tables."""

    # 1. Create the reusable trigger function.
    op.execute(
        sa.text("""
        CREATE OR REPLACE FUNCTION set_updated_at()
        RETURNS TRIGGER AS $$
        BEGIN
            NEW.updated_at = now();
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """)
    )

    # 2. Attach the trigger to every table that has an updated_at column.
    for table in TABLES_WITH_UPDATED_AT:
        op.execute(
            sa.text(f"""
            CREATE TRIGGER trg_{table}_set_updated_at
            BEFORE UPDATE ON {table}
            FOR EACH ROW
            EXECUTE FUNCTION set_updated_at();
            """)
        )


def downgrade() -> None:
    """Drop triggers and the trigger function."""

    for table in reversed(TABLES_WITH_UPDATED_AT):
        op.execute(
            sa.text(f"DROP TRIGGER IF EXISTS trg_{table}_set_updated_at ON {table};")
        )

    op.execute(sa.text("DROP FUNCTION IF EXISTS set_updated_at();"))
