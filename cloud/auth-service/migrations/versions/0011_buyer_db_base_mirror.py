"""Make PostgreSQL authoritative and mirror buyer metadata to Feishu Base.

Revision ID: 0011_buyer_db_base_mirror
Revises: 0010_operation_result_sync
Create Date: 2026-08-26
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "0011_buyer_db_base_mirror"
down_revision: str | None = "0010_operation_result_sync"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "buyer_accounts", sa.Column("source_vendor_label", sa.String(length=100))
    )
    op.add_column(
        "buyer_accounts", sa.Column("source_batch_ref", sa.String(length=128))
    )
    op.add_column("buyer_accounts", sa.Column("source_purchase_date", sa.Date()))
    op.add_column(
        "buyer_accounts", sa.Column("source_order_ref", sa.String(length=128))
    )
    op.add_column(
        "buyer_accounts",
        sa.Column(
            "feishu_sync_status",
            sa.String(length=32),
            server_default="pending",
            nullable=False,
        ),
    )
    op.add_column(
        "buyer_accounts", sa.Column("feishu_record_id", sa.String(length=128))
    )
    op.add_column(
        "buyer_accounts", sa.Column("feishu_synced_at", sa.DateTime(timezone=True))
    )
    op.alter_column("buyer_accounts", "feishu_sync_status", server_default=None)
    op.create_unique_constraint(
        "uq_buyer_account_tenant_source_order",
        "buyer_accounts",
        ["tenant_id", "source_order_ref"],
    )
    op.create_check_constraint(
        "ck_buyer_account_feishu_status",
        "buyer_accounts",
        "feishu_sync_status IN ('pending', 'processing', 'completed', 'failed')",
    )

    op.drop_constraint(
        "ck_operational_sync_aggregate_type",
        "operational_sync_outbox",
        type_="check",
    )
    op.create_check_constraint(
        "ck_operational_sync_aggregate_type",
        "operational_sync_outbox",
        "aggregate_type IN ('buyer_account', 'environment_creation_result', "
        "'logistics_query_result')",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_operational_sync_aggregate_type",
        "operational_sync_outbox",
        type_="check",
    )
    op.create_check_constraint(
        "ck_operational_sync_aggregate_type",
        "operational_sync_outbox",
        "aggregate_type IN ('environment_creation_result', 'logistics_query_result')",
    )

    op.drop_constraint(
        "ck_buyer_account_feishu_status", "buyer_accounts", type_="check"
    )
    op.drop_constraint(
        "uq_buyer_account_tenant_source_order",
        "buyer_accounts",
        type_="unique",
    )
    op.drop_column("buyer_accounts", "feishu_synced_at")
    op.drop_column("buyer_accounts", "feishu_record_id")
    op.drop_column("buyer_accounts", "feishu_sync_status")
    op.drop_column("buyer_accounts", "source_order_ref")
    op.drop_column("buyer_accounts", "source_purchase_date")
    op.drop_column("buyer_accounts", "source_batch_ref")
    op.drop_column("buyer_accounts", "source_vendor_label")
