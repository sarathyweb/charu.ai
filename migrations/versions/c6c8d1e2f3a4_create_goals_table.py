"""create_goals_table

Revision ID: c6c8d1e2f3a4
Revises: b4c8e2f1a937
Create Date: 2026-04-25

"""

from collections.abc import Sequence

import sqlalchemy as sa
import sqlmodel
from alembic import op

revision: str = "c6c8d1e2f3a4"
down_revision: str | Sequence[str] | None = "b4c8e2f1a937"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "goals",
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "status",
            sqlmodel.sql.sqltypes.AutoString(),
            nullable=False,
            server_default="active",
        ),
        sa.Column("target_date", sa.Date(), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('active', 'completed', 'abandoned')",
            name="ck_goal_status",
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_goals_user_id"), "goals", ["user_id"], unique=False)
    op.create_index(
        "ix_goals_user_status_created",
        "goals",
        ["user_id", "status", "created_at"],
        unique=False,
    )
    op.execute(
        sa.text("""
        CREATE TRIGGER trg_goals_set_updated_at
        BEFORE UPDATE ON goals
        FOR EACH ROW
        EXECUTE FUNCTION set_updated_at();
        """)
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.execute(sa.text("DROP TRIGGER IF EXISTS trg_goals_set_updated_at ON goals;"))
    op.drop_index("ix_goals_user_status_created", table_name="goals")
    op.drop_index(op.f("ix_goals_user_id"), table_name="goals")
    op.drop_table("goals")
