"""add_updated_at_triggers_for_call_logs_email_drafts_outbound

Revision ID: e49dd82c3481
Revises: a1148ca21520
Create Date: 2026-04-06 06:52:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e49dd82c3481'
down_revision: Union[str, Sequence[str], None] = 'a1148ca21520'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Tables that use TimestampMixin but were missing the set_updated_at trigger.
_TABLES = ("call_logs", "email_draft_states", "outbound_messages")


def upgrade() -> None:
    """Attach the reusable set_updated_at() trigger to tables that were missed."""
    for table in _TABLES:
        op.execute(sa.text(f"""
            CREATE TRIGGER trg_{table}_set_updated_at
            BEFORE UPDATE ON {table}
            FOR EACH ROW
            EXECUTE FUNCTION set_updated_at();
        """))


def downgrade() -> None:
    """Remove the triggers added by this migration."""
    for table in _TABLES:
        op.execute(sa.text(f"DROP TRIGGER IF EXISTS trg_{table}_set_updated_at ON {table};"))
