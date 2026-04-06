"""add_call_window_time_check_constraints

Revision ID: a1148ca21520
Revises: 0bd09a1e5bb6
Create Date: 2026-04-06 06:49:42.123784

"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'a1148ca21520'
down_revision: Union[str, Sequence[str], None] = '0bd09a1e5bb6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add DB-level check constraints for call window time validity.

    Mirrors the Python-level validation in call_window_validation.py:
    1. end_time must be strictly after start_time (no cross-midnight)
    2. Window must be at least 20 minutes wide
    """
    op.create_check_constraint(
        "ck_call_window_no_cross_midnight",
        "call_windows",
        "end_time > start_time",
    )
    op.create_check_constraint(
        "ck_call_window_min_width_20min",
        "call_windows",
        "(EXTRACT(HOUR FROM end_time) * 60 + EXTRACT(MINUTE FROM end_time)) - "
        "(EXTRACT(HOUR FROM start_time) * 60 + EXTRACT(MINUTE FROM start_time)) >= 20",
    )


def downgrade() -> None:
    """Remove the time validation check constraints."""
    op.drop_constraint("ck_call_window_min_width_20min", "call_windows", type_="check")
    op.drop_constraint("ck_call_window_no_cross_midnight", "call_windows", type_="check")
