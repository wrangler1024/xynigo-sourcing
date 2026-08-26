"""Add checkout-safe buyer account inventory and canonical bindings.

Revision ID: 0009_buyer_account_resource
Revises: 0008_checkout_flow
Create Date: 2026-08-26
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "0009_buyer_account_resource"
down_revision: str | None = "0008_checkout_flow"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "buyer_accounts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("account_ref", sa.String(length=128), nullable=False),
        sa.Column("display_label", sa.String(length=255), nullable=False),
        sa.Column("site", sa.String(length=20), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("source_availability_status", sa.String(length=32), nullable=False),
        sa.Column("credential_status", sa.String(length=32), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("source_status", sa.String(length=64)),
        sa.Column("hub_environment_ref", sa.String(length=128)),
        sa.Column("hub_environment_name", sa.String(length=255)),
        sa.Column("operator_label", sa.String(length=100)),
        sa.Column("current_checkout_attempt_id", sa.Uuid()),
        sa.Column("last_snapshot_key", sa.String(length=128)),
        sa.Column("source_updated_at", sa.DateTime(timezone=True)),
        sa.Column(
            "last_synced_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
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
        sa.CheckConstraint("site IN ('US', 'MX')", name="ck_buyer_account_site"),
        sa.CheckConstraint(
            "status IN ('available', 'reserved', 'in_use', 'cleanup_pending', "
            "'post_payment_hold', 'manual_review', 'disabled')",
            name="ck_buyer_account_status",
        ),
        sa.CheckConstraint(
            "source_availability_status IN ('available', 'manual_review', 'disabled')",
            name="ck_buyer_account_source_availability_status",
        ),
        sa.CheckConstraint(
            "credential_status IN ('ready', 'unverified', 'invalid', 'unknown')",
            name="ck_buyer_account_credential_status",
        ),
        sa.CheckConstraint("version >= 1", name="ck_buyer_account_version"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["current_checkout_attempt_id"],
            ["checkout_attempts.id"],
            name="fk_buyer_account_current_attempt",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id", "account_ref", name="uq_buyer_account_tenant_ref"
        ),
        sa.UniqueConstraint(
            "current_checkout_attempt_id", name="uq_buyer_account_current_attempt"
        ),
    )
    op.create_index(
        "ix_buyer_account_tenant_site_status",
        "buyer_accounts",
        ["tenant_id", "site", "status", "updated_at"],
    )
    op.create_index(
        "ix_buyer_account_tenant_credential",
        "buyer_accounts",
        ["tenant_id", "credential_status", "status"],
    )

    op.add_column("checkout_attempts", sa.Column("buyer_account_id", sa.Uuid()))
    op.create_foreign_key(
        "fk_checkout_attempt_buyer_account",
        "checkout_attempts",
        "buyer_accounts",
        ["buyer_account_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        "ix_checkout_attempt_buyer_account",
        "checkout_attempts",
        ["buyer_account_id", "status"],
    )
    op.drop_constraint(
        "ck_checkout_attempt_resource_pair", "checkout_attempts", type_="check"
    )
    op.create_check_constraint(
        "ck_checkout_attempt_resource_pair",
        "checkout_attempts",
        "(hub_environment_ref IS NULL AND buyer_account_id IS NULL AND "
        "buyer_account_ref IS NULL) OR "
        "(hub_environment_ref IS NOT NULL AND buyer_account_id IS NOT NULL AND "
        "buyer_account_ref IS NOT NULL)",
    )

    op.add_column("purchase_batches", sa.Column("buyer_account_id", sa.Uuid()))
    op.create_foreign_key(
        "fk_purchase_batch_buyer_account",
        "purchase_batches",
        "buyer_accounts",
        ["buyer_account_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        "ix_purchase_batch_buyer_account",
        "purchase_batches",
        ["buyer_account_id", "paid_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_purchase_batch_buyer_account", table_name="purchase_batches")
    op.drop_constraint(
        "fk_purchase_batch_buyer_account", "purchase_batches", type_="foreignkey"
    )
    op.drop_column("purchase_batches", "buyer_account_id")

    op.drop_constraint(
        "ck_checkout_attempt_resource_pair", "checkout_attempts", type_="check"
    )
    op.create_check_constraint(
        "ck_checkout_attempt_resource_pair",
        "checkout_attempts",
        "(hub_environment_ref IS NULL AND buyer_account_ref IS NULL) OR "
        "(hub_environment_ref IS NOT NULL AND buyer_account_ref IS NOT NULL)",
    )
    op.drop_index("ix_checkout_attempt_buyer_account", table_name="checkout_attempts")
    op.drop_constraint(
        "fk_checkout_attempt_buyer_account", "checkout_attempts", type_="foreignkey"
    )
    op.drop_column("checkout_attempts", "buyer_account_id")

    op.drop_index(
        "ix_buyer_account_tenant_credential", table_name="buyer_accounts"
    )
    op.drop_index(
        "ix_buyer_account_tenant_site_status", table_name="buyer_accounts"
    )
    op.drop_table("buyer_accounts")
