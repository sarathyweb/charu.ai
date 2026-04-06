"""add_claim_token_and_sending_status_to_outbound_messages

Revision ID: f3a1b7c9d2e4
Revises: e49dd82c3481
Create Date: 2026-04-06 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "f3a1b7c9d2e4"
down_revision: Union[str, Sequence[str], None] = "e49dd82c3481"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add claim_token column and 'sending' status for ownership fencing.

    1. Add claim_token (nullable) for fencing stale workers.
    2. Replace the status check constraint to include 'sending'.
    """
    op.add_column(
        "outbound_messages",
        sa.Column("claim_token", sa.String(), nullable=True),
    )
    # Widen the check constraint to include the new 'sending' status
    op.drop_constraint(
        "ck_outbound_message_status", "outbound_messages", type_="check"
    )
    op.create_check_constraint(
        "ck_outbound_message_status",
        "outbound_messages",
        "status IN ('pending', 'sending', 'sent', 'failed')",
    )


def downgrade() -> None:
    # Restore original check constraint (without 'sending')
    op.drop_constraint(
        "ck_outbound_message_status", "outbound_messages", type_="check"
    )
    op.create_check_constraint(
        "ck_outbound_message_status",
        "outbound_messages",
        "status IN ('pending', 'sent', 'failed')",
    )
    op.drop_column("outbound_messages", "claim_token")
