"""Add encrypted organization data-source registry.

Revision ID: 0031_data_source_registry_sync
Revises: 0030_logistics_auto_site
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision: str = "0031_data_source_registry_sync"
down_revision: str | None = "0030_logistics_auto_site"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "tenant_data_source_registries",
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("payload_ciphertext", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("source_executor_id", sa.Uuid()),
        sa.Column("updated_by_user_id", sa.Uuid()),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint("revision >= 1", name="ck_tenant_data_source_revision"),
        sa.CheckConstraint(
            "length(content_hash) = 64", name="ck_tenant_data_source_hash"
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["source_executor_id"], ["local_executors.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["updated_by_user_id"], ["users.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("tenant_id"),
    )
    op.alter_column("tenant_data_source_registries", "revision", server_default=None)


def downgrade() -> None:
    op.drop_table("tenant_data_source_registries")
