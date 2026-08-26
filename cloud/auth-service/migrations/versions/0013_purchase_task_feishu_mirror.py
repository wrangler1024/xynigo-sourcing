"""Add durable Feishu mirror state for purchase orders and lines.

Revision ID: 0013_purchase_task_feishu_mirror
Revises: 0012_buyer_account_credentials
Create Date: 2026-08-26
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "0013_purchase_task_feishu_mirror"
down_revision: str | None = "0012_buyer_account_credentials"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "purchase_orders", sa.Column("feishu_record_id", sa.String(length=128))
    )
    op.add_column(
        "purchase_orders", sa.Column("feishu_synced_at", sa.DateTime(timezone=True))
    )
    op.add_column(
        "purchase_orders", sa.Column("sync_error_code", sa.String(length=128))
    )
    op.create_unique_constraint(
        "uq_purchase_order_feishu_record", "purchase_orders", ["feishu_record_id"]
    )

    op.add_column(
        "purchase_order_lines", sa.Column("feishu_record_id", sa.String(length=128))
    )
    op.add_column(
        "purchase_order_lines",
        sa.Column("feishu_synced_at", sa.DateTime(timezone=True)),
    )
    op.create_unique_constraint(
        "uq_purchase_line_feishu_record", "purchase_order_lines", ["feishu_record_id"]
    )

    op.add_column(
        "purchase_sync_outbox", sa.Column("last_error_code", sa.String(length=128))
    )
    op.add_column(
        "purchase_sync_outbox",
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.drop_constraint(
        "ck_purchase_sync_event_type", "purchase_sync_outbox", type_="check"
    )
    op.create_check_constraint(
        "ck_purchase_sync_event_type",
        "purchase_sync_outbox",
        "event_type IN ('draft.saved', 'order.submitted', "
        "'order.assignment_changed', 'order.execution_changed', "
        "'checkout.attempted', 'checkout.updated', "
        "'checkout.started', 'checkout.abandoned', 'checkout.failed', "
        "'purchase.paid', 'shipment.updated')",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_purchase_sync_event_type", "purchase_sync_outbox", type_="check"
    )
    op.create_check_constraint(
        "ck_purchase_sync_event_type",
        "purchase_sync_outbox",
        "event_type IN ('draft.saved', 'order.submitted', 'checkout.attempted', "
        "'checkout.updated', 'checkout.started', 'checkout.abandoned', "
        "'checkout.failed', 'purchase.paid', 'shipment.updated')",
    )
    op.drop_column("purchase_sync_outbox", "updated_at")
    op.drop_column("purchase_sync_outbox", "last_error_code")

    op.drop_constraint(
        "uq_purchase_line_feishu_record", "purchase_order_lines", type_="unique"
    )
    op.drop_column("purchase_order_lines", "feishu_synced_at")
    op.drop_column("purchase_order_lines", "feishu_record_id")

    op.drop_constraint(
        "uq_purchase_order_feishu_record", "purchase_orders", type_="unique"
    )
    op.drop_column("purchase_orders", "sync_error_code")
    op.drop_column("purchase_orders", "feishu_synced_at")
    op.drop_column("purchase_orders", "feishu_record_id")
