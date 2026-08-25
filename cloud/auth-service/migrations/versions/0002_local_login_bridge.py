"""Add one-time local executor login bridge.

Revision ID: 0002_local_login_bridge
Revises: 0001_identity_foundation
Create Date: 2026-08-24
"""

from typing import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "0002_local_login_bridge"
down_revision: str | None = "0001_identity_foundation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "local_login_requests",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("poll_token_hash", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("denial_code", sa.String(length=128), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('pending', 'approved', 'denied', 'consumed')",
            name="ck_local_login_status",
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("poll_token_hash"),
    )
    op.create_index(
        "ix_local_login_expiry",
        "local_login_requests",
        ["expires_at"],
        unique=False,
    )
    op.add_column(
        "oauth_login_attempts",
        sa.Column("local_login_request_id", sa.Uuid(), nullable=True),
    )
    op.create_foreign_key(
        "fk_oauth_login_attempt_local_login",
        "oauth_login_attempts",
        "local_login_requests",
        ["local_login_request_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_oauth_login_attempt_local_login",
        "oauth_login_attempts",
        type_="foreignkey",
    )
    op.drop_column("oauth_login_attempts", "local_login_request_id")
    op.drop_index("ix_local_login_expiry", table_name="local_login_requests")
    op.drop_table("local_login_requests")
