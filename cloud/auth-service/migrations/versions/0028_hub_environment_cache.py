"""Add complete HubStudio environment snapshot cache.

Revision ID: 0028_hub_environment_cache
Revises: 0027_logistics_first_tracking
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision: str = "0028_hub_environment_cache"
down_revision: str | None = "0027_logistics_first_tracking"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "hub_environment_observations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("environment_key", sa.String(length=71), nullable=False),
        sa.Column("environment_name", sa.String(length=255), nullable=False),
        sa.Column("environment_ref", sa.String(length=128)),
        sa.Column("environment_serial", sa.String(length=64)),
        sa.Column("environment_group", sa.String(length=255), nullable=False),
        sa.Column("site", sa.String(length=20)),
        sa.Column("source_order_ref", sa.String(length=71)),
        sa.Column("snapshot_revision", sa.String(length=64), nullable=False),
        sa.Column("last_observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "site IS NULL OR site IN ('US', 'MX')",
            name="ck_hub_observation_site",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id", "environment_key",
            name="uq_hub_observation_tenant_key",
        ),
    )
    op.create_index(
        "ix_hub_observation_tenant_order",
        "hub_environment_observations", ["tenant_id", "source_order_ref"],
    )
    op.create_index(
        "ix_hub_observation_tenant_name",
        "hub_environment_observations", ["tenant_id", "environment_name"],
    )
    op.create_index(
        "ix_hub_observation_tenant_snapshot",
        "hub_environment_observations", ["tenant_id", "snapshot_revision"],
    )
    op.create_table(
        "hub_environment_inventory_syncs",
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("executor_id", sa.Uuid()),
        sa.Column("snapshot_revision", sa.String(length=64), nullable=False),
        sa.Column("environment_count", sa.Integer(), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "environment_count >= 0", name="ck_hub_inventory_sync_count"
        ),
        sa.ForeignKeyConstraint(
            ["executor_id"], ["local_executors.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("tenant_id"),
    )
    op.create_index(
        "ix_hub_inventory_sync_completed",
        "hub_environment_inventory_syncs", ["completed_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_hub_inventory_sync_completed",
        table_name="hub_environment_inventory_syncs",
    )
    op.drop_table("hub_environment_inventory_syncs")
    op.drop_index(
        "ix_hub_observation_tenant_snapshot",
        table_name="hub_environment_observations",
    )
    op.drop_index(
        "ix_hub_observation_tenant_name",
        table_name="hub_environment_observations",
    )
    op.drop_index(
        "ix_hub_observation_tenant_order",
        table_name="hub_environment_observations",
    )
    op.drop_table("hub_environment_observations")
