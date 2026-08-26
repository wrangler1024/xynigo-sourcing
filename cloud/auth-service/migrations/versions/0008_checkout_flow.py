"""Add checkout attempts, paid batches and shipment tracking.

Revision ID: 0008_checkout_flow
Revises: 0007_system_runtime_logs_p1
Create Date: 2026-08-26
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "0008_checkout_flow"
down_revision: str | None = "0007_system_runtime_logs_p1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "checkout_attempts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("purchase_order_id", sa.Uuid(), nullable=False),
        sa.Column("purchaser_user_id", sa.Uuid(), nullable=False),
        sa.Column("attempt_no", sa.String(length=64), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("site", sa.String(length=20), nullable=False),
        sa.Column("hub_environment_ref", sa.String(length=128)),
        sa.Column("hub_environment_name", sa.String(length=255)),
        sa.Column("buyer_account_ref", sa.String(length=128)),
        sa.Column("buyer_account_label", sa.String(length=255)),
        sa.Column("resource_status", sa.String(length=32), nullable=False),
        sa.Column("pending_terminal_status", sa.String(length=32)),
        sa.Column("note", sa.String(length=1000)),
        sa.Column("terminal_reason", sa.String(length=500)),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("payment_recorded_at", sa.DateTime(timezone=True)),
        sa.Column("terminal_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('planning', 'ready', 'checkout', 'cleanup_pending', "
            "'manual_review', 'paid', 'failed', 'abandoned')",
            name="ck_checkout_attempt_status",
        ),
        sa.CheckConstraint(
            "resource_status IN ('unbound', 'reserved', 'active', 'cleanup_pending', "
            "'released', 'retained', 'manual_review')",
            name="ck_checkout_attempt_resource_status",
        ),
        sa.CheckConstraint(
            "pending_terminal_status IS NULL OR "
            "pending_terminal_status IN ('failed', 'abandoned')",
            name="ck_checkout_attempt_pending_terminal",
        ),
        sa.CheckConstraint("version >= 1", name="ck_checkout_attempt_version"),
        sa.CheckConstraint(
            "(hub_environment_ref IS NULL AND buyer_account_ref IS NULL) OR "
            "(hub_environment_ref IS NOT NULL AND buyer_account_ref IS NOT NULL)",
            name="ck_checkout_attempt_resource_pair",
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["purchase_order_id"], ["purchase_orders.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["purchaser_user_id"], ["users.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id", "attempt_no", name="uq_checkout_attempt_tenant_no"
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "idempotency_key",
            name="uq_checkout_attempt_tenant_idempotency",
        ),
    )
    op.create_index(
        "ix_checkout_attempt_order_status",
        "checkout_attempts",
        ["purchase_order_id", "status", "updated_at"],
    )
    op.create_index(
        "ix_checkout_attempt_purchaser_status",
        "checkout_attempts",
        ["purchaser_user_id", "status", "updated_at"],
    )
    active_attempt = sa.text(
        "status IN ('planning', 'ready', 'checkout', 'cleanup_pending', "
        "'manual_review')"
    )
    op.create_index(
        "uq_checkout_attempt_active_hub",
        "checkout_attempts",
        ["tenant_id", "hub_environment_ref"],
        unique=True,
        postgresql_where=active_attempt & sa.text("hub_environment_ref IS NOT NULL"),
        sqlite_where=active_attempt & sa.text("hub_environment_ref IS NOT NULL"),
    )
    op.create_index(
        "uq_checkout_attempt_active_buyer",
        "checkout_attempts",
        ["tenant_id", "buyer_account_ref"],
        unique=True,
        postgresql_where=active_attempt & sa.text("buyer_account_ref IS NOT NULL"),
        sqlite_where=active_attempt & sa.text("buyer_account_ref IS NOT NULL"),
    )

    op.create_table(
        "checkout_attempt_lines",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("checkout_attempt_id", sa.Uuid(), nullable=False),
        sa.Column("purchase_order_line_id", sa.Uuid(), nullable=False),
        sa.Column("reserved_qty", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint("reserved_qty >= 1", name="ck_checkout_attempt_line_qty"),
        sa.ForeignKeyConstraint(
            ["checkout_attempt_id"], ["checkout_attempts.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["purchase_order_line_id"],
            ["purchase_order_lines.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "checkout_attempt_id",
            "purchase_order_line_id",
            name="uq_checkout_attempt_line",
        ),
    )
    op.create_index(
        "ix_checkout_attempt_line_source",
        "checkout_attempt_lines",
        ["purchase_order_line_id"],
    )

    op.create_table(
        "purchase_batches",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("purchase_order_id", sa.Uuid(), nullable=False),
        sa.Column("checkout_attempt_id", sa.Uuid(), nullable=False),
        sa.Column("purchaser_user_id", sa.Uuid(), nullable=False),
        sa.Column("batch_no", sa.String(length=64), nullable=False),
        sa.Column("platform", sa.String(length=32), nullable=False),
        sa.Column("platform_order_no", sa.String(length=200), nullable=False),
        sa.Column("site", sa.String(length=20), nullable=False),
        sa.Column("actual_amount", sa.Numeric(18, 2), nullable=False),
        sa.Column("currency", sa.String(length=12), nullable=False),
        sa.Column("discount_amount", sa.Numeric(18, 2)),
        sa.Column("coupon_summary", sa.String(length=500)),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("hub_environment_ref", sa.String(length=128), nullable=False),
        sa.Column("hub_environment_name", sa.String(length=255), nullable=False),
        sa.Column("buyer_account_ref", sa.String(length=128), nullable=False),
        sa.Column("buyer_account_label", sa.String(length=255), nullable=False),
        sa.Column("paid_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('paid', 'tracking', 'completed', 'exception')",
            name="ck_purchase_batch_status",
        ),
        sa.CheckConstraint(
            "actual_amount >= 0", name="ck_purchase_batch_actual_amount"
        ),
        sa.CheckConstraint(
            "discount_amount IS NULL OR discount_amount >= 0",
            name="ck_purchase_batch_discount_amount",
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["purchase_order_id"], ["purchase_orders.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["checkout_attempt_id"], ["checkout_attempts.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["purchaser_user_id"], ["users.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("checkout_attempt_id", name="uq_purchase_batch_attempt"),
        sa.UniqueConstraint(
            "tenant_id", "batch_no", name="uq_purchase_batch_tenant_no"
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "platform",
            "platform_order_no",
            name="uq_purchase_batch_platform_order",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "hub_environment_ref",
            name="uq_purchase_batch_hub_environment",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "buyer_account_ref",
            name="uq_purchase_batch_buyer_account",
        ),
    )
    op.create_index(
        "ix_purchase_batch_order_status",
        "purchase_batches",
        ["purchase_order_id", "status", "updated_at"],
    )
    op.create_index(
        "ix_purchase_batch_purchaser_status",
        "purchase_batches",
        ["purchaser_user_id", "status", "updated_at"],
    )

    op.create_table(
        "purchase_batch_lines",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("purchase_batch_id", sa.Uuid(), nullable=False),
        sa.Column("purchase_order_line_id", sa.Uuid(), nullable=False),
        sa.Column("purchased_qty", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint("purchased_qty >= 1", name="ck_purchase_batch_line_qty"),
        sa.ForeignKeyConstraint(
            ["purchase_batch_id"], ["purchase_batches.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["purchase_order_line_id"],
            ["purchase_order_lines.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "purchase_batch_id",
            "purchase_order_line_id",
            name="uq_purchase_batch_line",
        ),
    )
    op.create_index(
        "ix_purchase_batch_line_source",
        "purchase_batch_lines",
        ["purchase_order_line_id"],
    )

    op.create_table(
        "supplier_shipments",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("purchase_batch_id", sa.Uuid(), nullable=False),
        sa.Column("shipment_key", sa.String(length=128), nullable=False),
        sa.Column("package_no", sa.String(length=200)),
        sa.Column("carrier_code", sa.String(length=64)),
        sa.Column("carrier_name", sa.String(length=128), nullable=False),
        sa.Column("tracking_no", sa.String(length=200), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("shipped_at", sa.DateTime(timezone=True)),
        sa.Column("delivered_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('pending_pickup', 'in_transit', 'delivered', 'exception')",
            name="ck_supplier_shipment_status",
        ),
        sa.CheckConstraint("version >= 1", name="ck_supplier_shipment_version"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["purchase_batch_id"], ["purchase_batches.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "purchase_batch_id",
            "shipment_key",
            name="uq_supplier_shipment_batch_key",
        ),
        sa.UniqueConstraint(
            "purchase_batch_id",
            "tracking_no",
            name="uq_supplier_shipment_tracking",
        ),
    )
    op.create_index(
        "ix_supplier_shipment_batch_status",
        "supplier_shipments",
        ["purchase_batch_id", "status", "updated_at"],
    )

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


def downgrade() -> None:
    op.drop_constraint(
        "ck_purchase_sync_event_type", "purchase_sync_outbox", type_="check"
    )
    op.create_check_constraint(
        "ck_purchase_sync_event_type",
        "purchase_sync_outbox",
        "event_type IN ('draft.saved', 'order.submitted')",
    )
    op.drop_index("ix_supplier_shipment_batch_status", table_name="supplier_shipments")
    op.drop_table("supplier_shipments")
    op.drop_index("ix_purchase_batch_line_source", table_name="purchase_batch_lines")
    op.drop_table("purchase_batch_lines")
    op.drop_index("ix_purchase_batch_purchaser_status", table_name="purchase_batches")
    op.drop_index("ix_purchase_batch_order_status", table_name="purchase_batches")
    op.drop_table("purchase_batches")
    op.drop_index(
        "ix_checkout_attempt_line_source", table_name="checkout_attempt_lines"
    )
    op.drop_table("checkout_attempt_lines")
    op.drop_index(
        "ix_checkout_attempt_purchaser_status", table_name="checkout_attempts"
    )
    op.drop_index("uq_checkout_attempt_active_buyer", table_name="checkout_attempts")
    op.drop_index("uq_checkout_attempt_active_hub", table_name="checkout_attempts")
    op.drop_index("ix_checkout_attempt_order_status", table_name="checkout_attempts")
    op.drop_table("checkout_attempts")
