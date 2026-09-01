"""Add encrypted cloud environment plans and cloud-owned preferences.

Revision ID: 0019_environment_cloud_plan
Revises: 0018_operation_runs_v2
Create Date: 2026-09-01
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0019_environment_cloud_plan"
down_revision: str | None = "0018_operation_runs_v2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "environment_account_plans",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("created_by_user_id", sa.Uuid(), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("filename", sa.String(255), nullable=False),
        sa.Column("site", sa.String(8), nullable=False),
        sa.Column("environment_group", sa.String(255), nullable=False),
        sa.Column("source_hash", sa.String(64), nullable=False),
        sa.Column(
            "preview_summary", sa.JSON(), nullable=False, server_default=sa.text("'{}'")
        ),
        sa.Column("encrypted_payload", sa.LargeBinary()),
        sa.Column("status", sa.String(32), nullable=False, server_default="parsed"),
        sa.Column("account_count", sa.Integer(), nullable=False),
        sa.Column("cookie_count", sa.Integer(), nullable=False),
        sa.Column("mixed_site_cookie_count", sa.Integer(), nullable=False),
        sa.Column("password_kind_count", sa.Integer(), nullable=False),
        sa.Column("order_count", sa.Integer(), nullable=False),
        sa.Column("submitted_at", sa.DateTime(timezone=True)),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
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
        sa.CheckConstraint("site IN ('US', 'MX')", name="ck_environment_account_plan_site"),
        sa.CheckConstraint(
            "status IN ('parsed', 'submitted', 'expired')",
            name="ck_environment_account_plan_status",
        ),
        sa.CheckConstraint(
            "account_count >= 1 AND cookie_count >= 0 AND "
            "mixed_site_cookie_count >= 0 AND password_kind_count >= 0 AND "
            "order_count >= 0",
            name="ck_environment_account_plan_counts",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"], ["users.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id",
            "created_by_user_id",
            "idempotency_key",
            name="uq_environment_account_plan_idempotency",
        ),
    )
    op.create_index(
        "ix_environment_account_plan_tenant_expiry",
        "environment_account_plans",
        ["tenant_id", "expires_at"],
    )
    op.create_index(
        "ix_environment_account_plan_user_latest",
        "environment_account_plans",
        ["tenant_id", "created_by_user_id", "created_at"],
    )
    op.create_index(
        "uq_environment_account_plan_active_source",
        "environment_account_plans",
        ["tenant_id", "created_by_user_id", "source_hash"],
        unique=True,
        postgresql_where=sa.text("status = 'parsed'"),
    )

    op.create_table(
        "environment_account_plan_requests",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("created_by_user_id", sa.Uuid(), nullable=False),
        sa.Column("cloud_plan_id", sa.Uuid(), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("source_hash", sa.String(64), nullable=False),
        sa.Column("reused", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"], ["users.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["cloud_plan_id"], ["environment_account_plans.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id",
            "created_by_user_id",
            "idempotency_key",
            name="uq_environment_account_plan_request_idempotency",
        ),
    )
    op.create_index(
        "ix_environment_account_plan_request_plan",
        "environment_account_plan_requests",
        ["cloud_plan_id", "created_at"],
    )

    op.create_table(
        "environment_workspace_preferences",
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("purchase_site", sa.String(8), nullable=False, server_default="MX"),
        sa.Column(
            "purchase_tags", sa.JSON(), nullable=False, server_default=sa.text("'{}'")
        ),
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
        sa.CheckConstraint(
            "purchase_site IN ('US', 'MX')",
            name="ck_environment_workspace_preference_site",
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("tenant_id", "user_id"),
    )


def downgrade() -> None:
    op.drop_table("environment_workspace_preferences")
    op.drop_index(
        "ix_environment_account_plan_request_plan",
        table_name="environment_account_plan_requests",
    )
    op.drop_table("environment_account_plan_requests")
    op.drop_index(
        "uq_environment_account_plan_active_source",
        table_name="environment_account_plans",
    )
    op.drop_index(
        "ix_environment_account_plan_user_latest",
        table_name="environment_account_plans",
    )
    op.drop_index(
        "ix_environment_account_plan_tenant_expiry",
        table_name="environment_account_plans",
    )
    op.drop_table("environment_account_plans")
