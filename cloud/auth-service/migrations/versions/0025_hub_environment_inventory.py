"""Add durable Hub environment inventory and atomic name sequences.

Revision ID: 0025_hub_environment_inventory
Revises: 0024_logistics_history
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision: str = "0025_hub_environment_inventory"
down_revision: str | None = "0024_logistics_history"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "environment_creation_runs",
        sa.Column("error_code", sa.String(length=128), nullable=True),
    )
    op.add_column(
        "environment_creation_runs",
        sa.Column("error_summary", sa.String(length=300), nullable=True),
    )
    op.create_table(
        "environment_name_sequences",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("site", sa.String(length=20), nullable=False),
        sa.Column("purchase_date", sa.String(length=8), nullable=False),
        sa.Column("purchaser_code", sa.String(length=16), nullable=False),
        sa.Column("last_value", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("site IN ('US', 'MX')", name="ck_environment_name_sequence_site"),
        sa.CheckConstraint("last_value >= 0", name="ck_environment_name_sequence_value"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id", "site", "purchase_date", "purchaser_code",
            name="uq_environment_name_sequence_scope",
        ),
    )
    op.create_index(
        "ix_environment_name_sequence_tenant",
        "environment_name_sequences", ["tenant_id", "updated_at"],
    )
    op.create_table(
        "hub_environment_inventory",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("account_ref", sa.String(length=128), nullable=False),
        sa.Column("source_order_ref", sa.String(length=128), nullable=True),
        sa.Column("environment_name", sa.String(length=255), nullable=False),
        sa.Column("environment_ref", sa.String(length=128), nullable=True),
        sa.Column("environment_serial", sa.String(length=64), nullable=True),
        sa.Column("site", sa.String(length=20), nullable=False),
        sa.Column("environment_group", sa.String(length=255), nullable=False),
        sa.Column("purchaser_label", sa.String(length=100), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("source_run_id", sa.Uuid(), nullable=True),
        sa.Column("last_observed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("site IN ('US', 'MX')", name="ck_hub_inventory_site"),
        sa.CheckConstraint(
            "state IN ('reserved', 'active', 'uncertain', 'deleted')",
            name="ck_hub_inventory_state",
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["source_run_id"], ["environment_creation_runs.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id", "account_ref", name="uq_hub_inventory_tenant_account"
        ),
        sa.UniqueConstraint(
            "tenant_id", "source_order_ref", name="uq_hub_inventory_tenant_order"
        ),
        sa.UniqueConstraint(
            "tenant_id", "environment_name", name="uq_hub_inventory_tenant_name"
        ),
    )
    op.create_index(
        "ix_hub_inventory_tenant_group_state", "hub_environment_inventory",
        ["tenant_id", "environment_group", "state", "updated_at"],
    )
    op.create_index(
        "ix_hub_inventory_source_run", "hub_environment_inventory",
        ["source_run_id", "state"],
    )

    # Seed the cache from the latest durable successful Xynigo results. Older
    # rows are intentionally retained in their source tables for audit.
    op.execute(sa.text("""
        WITH ranked AS (
            SELECT r.id, r.tenant_id, r.account_ref, b.source_order_ref,
                   r.environment_name, r.environment_ref,
                   r.environment_serial, run.site, run.environment_group,
                   r.purchaser_label, r.run_id, r.updated_at, r.created_at,
                   ROW_NUMBER() OVER (
                       PARTITION BY r.tenant_id, r.account_ref
                       ORDER BY r.updated_at DESC, r.id DESC
                   ) AS account_rank,
                   ROW_NUMBER() OVER (
                       PARTITION BY r.tenant_id, r.environment_name
                       ORDER BY r.updated_at DESC, r.id DESC
                   ) AS name_rank
              FROM environment_creation_results r
              JOIN environment_creation_runs run ON run.id = r.run_id
              LEFT JOIN buyer_accounts b
                ON b.tenant_id = r.tenant_id AND b.account_ref = r.account_ref
             WHERE r.status = 'success'
        )
        INSERT INTO hub_environment_inventory (
            id, tenant_id, account_ref, source_order_ref,
            environment_name, environment_ref,
            environment_serial, site, environment_group, purchaser_label,
            state, source_run_id, last_observed_at, created_at, updated_at
        )
        SELECT id, tenant_id, account_ref, source_order_ref,
               environment_name, environment_ref, environment_serial, site,
               environment_group, purchaser_label, 'active', run_id,
               updated_at, created_at, updated_at
          FROM ranked
         WHERE account_rank = 1 AND name_rank = 1
    """))


def downgrade() -> None:
    op.drop_index("ix_hub_inventory_source_run", table_name="hub_environment_inventory")
    op.drop_index("ix_hub_inventory_tenant_group_state", table_name="hub_environment_inventory")
    op.drop_table("hub_environment_inventory")
    op.drop_index("ix_environment_name_sequence_tenant", table_name="environment_name_sequences")
    op.drop_table("environment_name_sequences")
    op.drop_column("environment_creation_runs", "error_summary")
    op.drop_column("environment_creation_runs", "error_code")
