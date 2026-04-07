"""create initial tables (users)

Revision ID: 000000000001
Revises:
Create Date: 2026-04-06

Creates the base tables that were originally created by
SQLModel.metadata.create_all() on dev but need to exist before
subsequent migrations can add triggers, columns, etc.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel  # noqa: F401

revision: str = "000000000001"
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("phone", sa.String(), nullable=False),
        sa.Column("firebase_uid", sa.String(), nullable=True),
        sa.Column("name", sa.String(), nullable=True),
        sa.Column("timezone", sa.String(), nullable=True),
        sa.Column("onboarding_complete", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("google_access_token_encrypted", sa.String(), nullable=True),
        sa.Column("google_refresh_token_encrypted", sa.String(), nullable=True),
        sa.Column("google_token_expiry", sa.DateTime(timezone=True), nullable=True),
        sa.Column("google_granted_scopes", sa.String(), nullable=True),
        sa.Column("last_opener_id", sa.String(), nullable=True),
        sa.Column("last_approach", sa.String(), nullable=True),
        sa.Column("consecutive_active_days", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("last_active_date", sa.Date(), nullable=True),
        sa.Column("last_checkin_template", sa.String(), nullable=True),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_weekly_summary_sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_user_whatsapp_message_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_users_phone", "users", ["phone"], unique=True)
    op.create_index("ix_users_firebase_uid", "users", ["firebase_uid"], unique=True)

    # Also create processed_messages and current_sessions which are
    # referenced by the app but never had their own migration.
    op.create_table(
        "processed_messages",
        sa.Column("message_sid", sa.String(), primary_key=True),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )

    op.create_table(
        "current_sessions",
        sa.Column("phone", sa.String(), primary_key=True),
        sa.Column("session_id", sa.String(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_current_sessions_session_id", "current_sessions", ["session_id"])


def downgrade() -> None:
    op.drop_index("ix_current_sessions_session_id", table_name="current_sessions")
    op.drop_table("current_sessions")
    op.drop_table("processed_messages")
    op.drop_index("ix_users_firebase_uid", table_name="users")
    op.drop_index("ix_users_phone", table_name="users")
    op.drop_table("users")
