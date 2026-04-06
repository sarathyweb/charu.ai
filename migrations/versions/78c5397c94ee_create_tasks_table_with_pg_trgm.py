"""create_tasks_table_with_pg_trgm

Revision ID: 78c5397c94ee
Revises: 8a2e0b6728c1
Create Date: 2026-04-06

Enables the pg_trgm extension for fuzzy title matching and creates the
tasks table with a GIN trigram index on the title column.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel  # noqa: F401


revision: str = "78c5397c94ee"
down_revision: Union[str, Sequence[str], None] = "8a2e0b6728c1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Enable pg_trgm extension for fuzzy text matching (similarity()).
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")

    op.create_table(
        "tasks",
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("status", sqlmodel.sql.sqltypes.AutoString(), nullable=False, server_default="pending"),
        sa.Column("priority", sa.Integer(), nullable=False, server_default="50"),
        sa.Column("source", sqlmodel.sql.sqltypes.AutoString(), nullable=False, server_default="user_mention"),
        sa.Column("snoozed_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('pending', 'completed', 'snoozed')",
            name="ck_task_status",
        ),
        sa.CheckConstraint(
            "source IN ('user_mention', 'gmail', 'calendar', 'import')",
            name="ck_task_source",
        ),
        sa.CheckConstraint(
            "priority >= 0 AND priority <= 100",
            name="ck_task_priority_range",
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_tasks_user_id"), "tasks", ["user_id"], unique=False)

    # GIN trigram index for fast similarity() lookups during fuzzy dedup.
    op.execute(
        "CREATE INDEX idx_tasks_title_trgm ON tasks USING gin (title gin_trgm_ops)"
    )

    # Attach the reusable set_updated_at trigger to the new table.
    op.execute(
        sa.text("""
        CREATE TRIGGER trg_tasks_set_updated_at
        BEFORE UPDATE ON tasks
        FOR EACH ROW
        EXECUTE FUNCTION set_updated_at();
        """)
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.execute(sa.text("DROP TRIGGER IF EXISTS trg_tasks_set_updated_at ON tasks;"))
    op.execute("DROP INDEX IF EXISTS idx_tasks_title_trgm")
    op.drop_index(op.f("ix_tasks_user_id"), table_name="tasks")
    op.drop_table("tasks")
    op.execute("DROP EXTENSION IF EXISTS pg_trgm")
