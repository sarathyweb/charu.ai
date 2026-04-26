"""add email automation preferences and events

Revision ID: e6f7a8b9c0d1
Revises: d5a4c3b2e1f0
Create Date: 2026-04-26

Adds user opt-ins and thread-level automation dedupe for Gmail-triggered
calls and task creation.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "e6f7a8b9c0d1"
down_revision: Union[str, Sequence[str], None] = "d5a4c3b2e1f0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "users",
        sa.Column(
            "urgent_email_calls_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.add_column(
        "users",
        sa.Column(
            "auto_task_from_emails_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.add_column(
        "users",
        sa.Column(
            "email_automation_quiet_hours_start",
            sa.Time(timezone=False),
            nullable=False,
            server_default="21:00:00",
        ),
    )
    op.add_column(
        "users",
        sa.Column(
            "email_automation_quiet_hours_end",
            sa.Time(timezone=False),
            nullable=False,
            server_default="08:00:00",
        ),
    )

    op.create_table(
        "email_automation_events",
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(length=32), nullable=False),
        sa.Column("gmail_message_id", sa.String(length=256), nullable=False),
        sa.Column("gmail_thread_id", sa.String(length=256), nullable=False),
        sa.Column(
            "status",
            sa.String(),
            nullable=False,
            server_default="processing",
        ),
        sa.Column("reason", sa.String(length=512), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("task_id", sa.Integer(), nullable=True),
        sa.Column("call_log_id", sa.Integer(), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "event_type IN ('urgent_call', 'auto_task')",
            name="ck_email_automation_event_type",
        ),
        sa.CheckConstraint(
            "status IN ('processing', 'created', 'skipped', 'failed')",
            name="ck_email_automation_status",
        ),
        sa.ForeignKeyConstraint(["call_log_id"], ["call_logs.id"]),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "user_id",
            "event_type",
            "gmail_thread_id",
            name="uq_email_automation_user_event_thread",
        ),
    )
    op.create_index(
        op.f("ix_email_automation_events_user_id"),
        "email_automation_events",
        ["user_id"],
        unique=False,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(
        op.f("ix_email_automation_events_user_id"),
        table_name="email_automation_events",
    )
    op.drop_table("email_automation_events")
    op.drop_column("users", "email_automation_quiet_hours_end")
    op.drop_column("users", "email_automation_quiet_hours_start")
    op.drop_column("users", "auto_task_from_emails_enabled")
    op.drop_column("users", "urgent_email_calls_enabled")
