"""Add tenant-scoped purchase requests and a Feishu synchronization outbox.

Revision ID: 0003_purchase_request_foundation
Revises: 0002_local_login_bridge
Create Date: 2026-08-25
"""

from typing import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "0003_purchase_request_foundation"
down_revision: str | None = "0002_local_login_bridge"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "purchase_orders",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("order_key", sa.String(length=800), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("draft_payload", sa.JSON(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("draft_revision", sa.Integer(), nullable=False),
        sa.Column("submission_status", sa.String(length=32), nullable=False),
        sa.Column("sync_status", sa.String(length=32), nullable=False),
        sa.Column("created_by_user_id", sa.Uuid(), nullable=False),
        sa.Column("last_edited_by_user_id", sa.Uuid(), nullable=False),
        sa.Column("submitted_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.CheckConstraint("draft_revision >= 1", name="ck_purchase_order_revision"),
        sa.CheckConstraint(
            "submission_status IN ('draft', 'submitted')",
            name="ck_purchase_order_submission_status",
        ),
        sa.CheckConstraint(
            "sync_status IN ('pending', 'synced', 'failed', 'conflict')",
            name="ck_purchase_order_sync_status",
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"], ["users.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["last_edited_by_user_id"], ["users.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["submitted_by_user_id"], ["users.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "order_key", name="uq_purchase_order_tenant_key"),
    )
    op.create_index(
        "ix_purchase_order_tenant_updated",
        "purchase_orders",
        ["tenant_id", "updated_at"],
        unique=False,
    )
    op.create_table(
        "purchase_order_lines",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("purchase_order_id", sa.Uuid(), nullable=False),
        sa.Column("line_key", sa.String(length=900), nullable=False),
        sa.Column("line_no", sa.Integer(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("workflow_status", sa.String(length=32), nullable=False),
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
        sa.CheckConstraint("line_no >= 1", name="ck_purchase_line_number"),
        sa.CheckConstraint(
            "workflow_status IN "
            "('draft', 'unclaimed', 'claimed', 'purchasing', 'ordered', "
            "'logistics_filled', 'completed', 'returned', 'exception')",
            name="ck_purchase_line_workflow_status",
        ),
        sa.ForeignKeyConstraint(
            ["purchase_order_id"], ["purchase_orders.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "purchase_order_id", "line_key", name="uq_purchase_line_order_key"
        ),
        sa.UniqueConstraint(
            "purchase_order_id", "line_no", name="uq_purchase_line_order_no"
        ),
    )
    op.create_index(
        "ix_purchase_line_order_active",
        "purchase_order_lines",
        ["purchase_order_id", "is_active"],
        unique=False,
    )
    op.create_table(
        "purchase_sync_outbox",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("purchase_order_id", sa.Uuid(), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("dedupe_key", sa.String(length=255), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "event_type IN ('draft.saved', 'order.submitted')",
            name="ck_purchase_sync_event_type",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'processing', 'completed', 'failed')",
            name="ck_purchase_sync_status",
        ),
        sa.CheckConstraint("attempt_count >= 0", name="ck_purchase_sync_attempt_count"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["purchase_order_id"], ["purchase_orders.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("dedupe_key"),
    )
    op.create_index(
        "ix_purchase_sync_pending",
        "purchase_sync_outbox",
        ["status", "available_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_purchase_sync_pending", table_name="purchase_sync_outbox")
    op.drop_table("purchase_sync_outbox")
    op.drop_index("ix_purchase_line_order_active", table_name="purchase_order_lines")
    op.drop_table("purchase_order_lines")
    op.drop_index("ix_purchase_order_tenant_updated", table_name="purchase_orders")
    op.drop_table("purchase_orders")
