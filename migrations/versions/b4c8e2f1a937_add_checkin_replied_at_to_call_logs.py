"""add_checkin_replied_at_to_call_logs

Revision ID: b4c8e2f1a937
Revises: f3a1b7c9d2e4
Create Date: 2026-04-07 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "b4c8e2f1a937"
down_revision: Union[str, Sequence[str], None] = "f3a1b7c9d2e4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "call_logs",
        sa.Column("checkin_replied_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("call_logs", "checkin_replied_at")
