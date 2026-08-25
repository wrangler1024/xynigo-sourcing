"""Add procurement claims and purchase split test-version storage.

Revision ID: 0004_procurement_execution_test
Revises: 0003_purchase_request_foundation
Create Date: 2026-08-25
"""

from typing import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "0004_procurement_execution_test"
down_revision: str | None = "0003_purchase_request_foundation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "purchase_orders",
        sa.Column(
            "execution_revision",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )
    op.create_check_constraint(
        "ck_purchase_order_execution_revision",
        "purchase_orders",
        "execution_revision >= 0",
    )
    op.add_column(
        "purchase_order_lines",
        sa.Column("claimed_by_user_id", sa.Uuid(), nullable=True),
    )
    op.add_column(
        "purchase_order_lines",
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_purchase_line_claimed_by_user",
        "purchase_order_lines",
        "users",
        ["claimed_by_user_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_purchase_line_claimant_status",
        "purchase_order_lines",
        ["claimed_by_user_id", "workflow_status"],
        unique=False,
    )

    op.create_table(
        "purchase_splits",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("purchase_order_id", sa.Uuid(), nullable=False),
        sa.Column("split_no", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("purchaser_user_id", sa.Uuid(), nullable=False),
        sa.Column("site", sa.String(length=20), nullable=False),
        sa.Column("hub_environment_ref", sa.String(length=128), nullable=True),
        sa.Column("hub_environment_name", sa.String(length=255), nullable=True),
        sa.Column("buyer_account_ref", sa.String(length=128), nullable=True),
        sa.Column("buyer_account_label", sa.String(length=255), nullable=True),
        sa.Column("platform_order_no", sa.String(length=200), nullable=True),
        sa.Column("note", sa.String(length=1000), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
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
            "status IN ('waiting_binding', 'waiting_order', 'purchasing', "
            "'ordered', 'exception')",
            name="ck_purchase_split_status",
        ),
        sa.CheckConstraint("version >= 1", name="ck_purchase_split_version"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["purchase_order_id"], ["purchase_orders.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["purchaser_user_id"], ["users.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "split_no", name="uq_purchase_split_tenant_no"),
    )
    op.create_index(
        "ix_purchase_split_tenant_status",
        "purchase_splits",
        ["tenant_id", "status", "updated_at"],
        unique=False,
    )
    op.create_index(
        "ix_purchase_split_order_purchaser",
        "purchase_splits",
        ["purchase_order_id", "purchaser_user_id"],
        unique=False,
    )

    op.create_table(
        "purchase_split_lines",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("purchase_split_id", sa.Uuid(), nullable=False),
        sa.Column("purchase_order_line_id", sa.Uuid(), nullable=False),
        sa.Column("allocated_qty", sa.Integer(), nullable=False),
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
        sa.CheckConstraint("allocated_qty >= 1", name="ck_purchase_split_line_qty"),
        sa.ForeignKeyConstraint(
            ["purchase_split_id"], ["purchase_splits.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["purchase_order_line_id"],
            ["purchase_order_lines.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "purchase_split_id",
            "purchase_order_line_id",
            name="uq_purchase_split_line",
        ),
    )
    op.create_index(
        "ix_purchase_split_line_source",
        "purchase_split_lines",
        ["purchase_order_line_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_purchase_split_line_source", table_name="purchase_split_lines")
    op.drop_table("purchase_split_lines")
    op.drop_index("ix_purchase_split_order_purchaser", table_name="purchase_splits")
    op.drop_index("ix_purchase_split_tenant_status", table_name="purchase_splits")
    op.drop_table("purchase_splits")
    op.drop_index("ix_purchase_line_claimant_status", table_name="purchase_order_lines")
    op.drop_constraint(
        "fk_purchase_line_claimed_by_user",
        "purchase_order_lines",
        type_="foreignkey",
    )
    op.drop_column("purchase_order_lines", "claimed_at")
    op.drop_column("purchase_order_lines", "claimed_by_user_id")
    op.drop_constraint(
        "ck_purchase_order_execution_revision",
        "purchase_orders",
        type_="check",
    )
    op.drop_column("purchase_orders", "execution_revision")
