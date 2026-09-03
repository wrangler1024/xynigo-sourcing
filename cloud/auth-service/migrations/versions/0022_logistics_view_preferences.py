"""Persist temporary logistics screenshots and reusable view preferences.

Revision ID: 0022_logistics_view_preferences
Revises: 0021_env_run_guard
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision: str = "0022_logistics_view_preferences"
down_revision: str | None = "0021_env_run_guard"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("logistics_query_results", sa.Column("screenshot_content", sa.LargeBinary()))
    op.add_column("logistics_query_results", sa.Column("screenshot_content_type", sa.String(length=64)))
    op.add_column("logistics_query_results", sa.Column("screenshot_sha256", sa.String(length=64)))
    op.add_column("logistics_query_results", sa.Column("screenshot_size", sa.Integer()))
    op.add_column("logistics_query_results", sa.Column("screenshot_expires_at", sa.DateTime(timezone=True)))
    op.create_table(
        "workspace_view_preferences",
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("view_key", sa.String(length=64), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("settings", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("schema_version >= 1", name="ck_workspace_view_preference_schema"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("tenant_id", "user_id", "view_key"),
    )


def downgrade() -> None:
    op.drop_table("workspace_view_preferences")
    op.drop_column("logistics_query_results", "screenshot_expires_at")
    op.drop_column("logistics_query_results", "screenshot_size")
    op.drop_column("logistics_query_results", "screenshot_sha256")
    op.drop_column("logistics_query_results", "screenshot_content_type")
    op.drop_column("logistics_query_results", "screenshot_content")
