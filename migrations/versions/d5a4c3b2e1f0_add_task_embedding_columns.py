"""add task embedding columns

Revision ID: d5a4c3b2e1f0
Revises: c6c8d1e2f3a4
Create Date: 2026-04-26

Stores Azure OpenAI task embeddings for semantic duplicate detection.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "d5a4c3b2e1f0"
down_revision: Union[str, Sequence[str], None] = "c6c8d1e2f3a4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "tasks",
        sa.Column("embedding", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column(
        "tasks",
        sa.Column("embedding_model", sa.String(length=128), nullable=True),
    )
    op.add_column(
        "tasks",
        sa.Column("embedding_updated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_tasks_user_status_embedding_model",
        "tasks",
        ["user_id", "status", "embedding_model"],
        unique=False,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_tasks_user_status_embedding_model", table_name="tasks")
    op.drop_column("tasks", "embedding_updated_at")
    op.drop_column("tasks", "embedding_model")
    op.drop_column("tasks", "embedding")
