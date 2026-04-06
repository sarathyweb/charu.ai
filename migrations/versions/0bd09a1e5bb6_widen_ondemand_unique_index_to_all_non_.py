"""widen_ondemand_unique_index_to_all_non_terminal_states

Revision ID: 0bd09a1e5bb6
Revises: 0719eaccbdef
Create Date: 2026-04-06 06:45:50.943634

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0bd09a1e5bb6'
down_revision: Union[str, Sequence[str], None] = '0719eaccbdef'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Widen the on-demand unique index to cover all non-terminal states.

    The old index only prevented duplicates while status='scheduled'.
    Once a row moved to dispatching/ringing/in_progress, a second
    scheduled on-demand row could be inserted — violating the
    "one active on-demand call per user" invariant.
    """
    op.drop_index(
        "ix_call_log_ondemand_unique",
        table_name="call_logs",
    )
    op.execute(
        "CREATE UNIQUE INDEX ix_call_log_ondemand_unique "
        "ON call_logs (user_id) "
        "WHERE call_type = 'on_demand' "
        "AND status NOT IN ('completed', 'missed', 'cancelled', 'skipped', 'deferred')"
    )


def downgrade() -> None:
    """Restore the original narrow index (scheduled-only)."""
    op.drop_index(
        "ix_call_log_ondemand_unique",
        table_name="call_logs",
    )
    op.execute(
        "CREATE UNIQUE INDEX ix_call_log_ondemand_unique "
        "ON call_logs (user_id) "
        "WHERE call_type = 'on_demand' AND status = 'scheduled'"
    )
