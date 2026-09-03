"""Add tenant-owned encrypted Feishu enterprise-app credentials.

Revision ID: 0023_tenant_feishu_integration
Revises: 0022_logistics_view_preferences
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision: str = "0023_tenant_feishu_integration"
down_revision: str | None = "0022_logistics_view_preferences"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "tenant_feishu_integrations",
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("app_id", sa.String(length=128), nullable=False),
        sa.Column("credential_ciphertext", sa.Text(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("configured_by_user_id", sa.Uuid()),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("revision >= 1", name="ck_tenant_feishu_integration_revision"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["configured_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("tenant_id"),
    )
    op.alter_column("tenant_feishu_integrations", "revision", server_default=None)


def downgrade() -> None:
    op.drop_table("tenant_feishu_integrations")
