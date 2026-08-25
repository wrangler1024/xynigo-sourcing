"""Persist store and operator attribution on purchase orders.

Revision ID: 0006_purchase_operator
Revises: 0004_procurement_execution_test
Create Date: 2026-08-26
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "0006_purchase_operator"
down_revision: str | None = "0004_procurement_execution_test"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("purchase_orders", sa.Column("store_name", sa.String(length=300)))
    op.add_column("purchase_orders", sa.Column("store_base_name", sa.String(length=300)))
    op.add_column("purchase_orders", sa.Column("operator_name", sa.String(length=100)))

    op.execute(
        """
        UPDATE purchase_orders
        SET store_name = COALESCE(
            NULLIF(BTRIM(draft_payload ->> 'storeName'), ''),
            '未知店铺'
        )
        """
    )
    op.execute(
        """
        WITH parsed AS (
            SELECT
                id,
                regexp_match(
                    store_name,
                    '^(.*)-([^-（）()]+)[（(][^（）()]*[）)][[:space:]]*[$¥￥]?[[:space:]]*$'
                ) AS parts
            FROM purchase_orders
        )
        UPDATE purchase_orders AS purchase_order
        SET store_base_name = NULLIF(BTRIM(parsed.parts[1]), ''),
            operator_name = NULLIF(BTRIM(parsed.parts[2]), '')
        FROM parsed
        WHERE purchase_order.id = parsed.id
          AND parsed.parts IS NOT NULL
        """
    )
    op.execute(
        """
        UPDATE purchase_orders
        SET store_base_name = store_name
        WHERE store_base_name IS NULL OR BTRIM(store_base_name) = ''
        """
    )

    op.alter_column(
        "purchase_orders",
        "store_name",
        existing_type=sa.String(length=300),
        nullable=False,
    )
    op.alter_column(
        "purchase_orders",
        "store_base_name",
        existing_type=sa.String(length=300),
        nullable=False,
    )
    op.create_index(
        "ix_purchase_order_tenant_operator_updated",
        "purchase_orders",
        ["tenant_id", "operator_name", "updated_at"],
    )
    op.create_index(
        "ix_purchase_order_tenant_store_base",
        "purchase_orders",
        ["tenant_id", "store_base_name"],
    )


def downgrade() -> None:
    op.drop_index("ix_purchase_order_tenant_store_base", table_name="purchase_orders")
    op.drop_index(
        "ix_purchase_order_tenant_operator_updated",
        table_name="purchase_orders",
    )
    op.drop_column("purchase_orders", "operator_name")
    op.drop_column("purchase_orders", "store_base_name")
    op.drop_column("purchase_orders", "store_name")
