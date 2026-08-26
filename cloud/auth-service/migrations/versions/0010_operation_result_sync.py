"""Persist daily environment creation and logistics query results.

Revision ID: 0010_operation_result_sync
Revises: 0009_buyer_account_resource
Create Date: 2026-08-26
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "0010_operation_result_sync"
down_revision: str | None = "0009_buyer_account_resource"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "environment_creation_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("actor_user_id", sa.Uuid(), nullable=False),
        sa.Column("source_run_key", sa.String(length=128), nullable=False),
        sa.Column("payload_hash", sa.String(length=64), nullable=False),
        sa.Column("site", sa.String(length=20), nullable=False),
        sa.Column("purchase_date", sa.String(length=8), nullable=False),
        sa.Column("environment_group", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("total_count", sa.Integer(), nullable=False),
        sa.Column("success_count", sa.Integer(), nullable=False),
        sa.Column("failed_count", sa.Integer(), nullable=False),
        sa.Column("ip_ok_count", sa.Integer(), nullable=False),
        sa.Column("ip_total_count", sa.Integer(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source", sa.String(length=64), nullable=False),
        sa.Column("client_version", sa.String(length=64)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("site IN ('US', 'MX')", name="ck_environment_run_site"),
        sa.CheckConstraint(
            "status IN ('completed', 'partial_failure', 'failed')",
            name="ck_environment_run_status",
        ),
        sa.CheckConstraint(
            "total_count >= 0 AND success_count >= 0 AND failed_count >= 0",
            name="ck_environment_run_counts",
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["actor_user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id", "source_run_key", name="uq_environment_run_tenant_source"
        ),
    )
    op.create_index(
        "ix_environment_run_tenant_completed",
        "environment_creation_runs",
        ["tenant_id", "completed_at"],
    )

    op.create_table(
        "environment_creation_results",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("account_ref", sa.String(length=128), nullable=False),
        sa.Column("account_label", sa.String(length=255), nullable=False),
        sa.Column("purchaser_label", sa.String(length=100), nullable=False),
        sa.Column("environment_name", sa.String(length=255), nullable=False),
        sa.Column("environment_ref", sa.String(length=128)),
        sa.Column("environment_serial", sa.String(length=64)),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("error_step", sa.String(length=64)),
        sa.Column("error_summary", sa.String(length=300)),
        sa.Column("binding_at", sa.DateTime(timezone=True)),
        sa.Column("recovered_existing", sa.Boolean(), nullable=False),
        sa.Column("ip_address", sa.String(length=64)),
        sa.Column("ip_country", sa.String(length=100)),
        sa.Column("ip_city", sa.String(length=100)),
        sa.Column("ip_isp", sa.String(length=200)),
        sa.Column("ip_verified", sa.Boolean()),
        sa.Column("feishu_sync_status", sa.String(length=32), nullable=False),
        sa.Column("feishu_record_id", sa.String(length=128)),
        sa.Column("feishu_synced_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "status IN ('success', 'failed')", name="ck_environment_result_status"
        ),
        sa.CheckConstraint(
            "feishu_sync_status IN ('pending', 'processing', 'completed', 'failed')",
            name="ck_environment_result_feishu_status",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"], ["environment_creation_runs.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_id", "account_ref", name="uq_environment_result_run_account"),
    )
    op.create_index(
        "ix_environment_result_tenant_created",
        "environment_creation_results",
        ["tenant_id", "created_at"],
    )
    op.create_index(
        "ix_environment_result_environment_ref",
        "environment_creation_results",
        ["tenant_id", "environment_ref"],
    )

    op.create_table(
        "logistics_query_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("actor_user_id", sa.Uuid(), nullable=False),
        sa.Column("source_run_key", sa.String(length=128), nullable=False),
        sa.Column("payload_hash", sa.String(length=64), nullable=False),
        sa.Column("query_mode", sa.String(length=32), nullable=False),
        sa.Column("site", sa.String(length=20), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("total_count", sa.Integer(), nullable=False),
        sa.Column("success_count", sa.Integer(), nullable=False),
        sa.Column("failed_count", sa.Integer(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source", sa.String(length=64), nullable=False),
        sa.Column("client_version", sa.String(length=64)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("site IN ('US', 'MX')", name="ck_logistics_run_site"),
        sa.CheckConstraint(
            "query_mode IN ('initial', 'single_retry', 'failed_retry')",
            name="ck_logistics_run_mode",
        ),
        sa.CheckConstraint(
            "status IN ('completed', 'partial_failure', 'failed')",
            name="ck_logistics_run_status",
        ),
        sa.CheckConstraint(
            "total_count >= 0 AND success_count >= 0 AND failed_count >= 0",
            name="ck_logistics_run_counts",
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["actor_user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id", "source_run_key", name="uq_logistics_run_tenant_source"
        ),
    )
    op.create_index(
        "ix_logistics_run_tenant_completed",
        "logistics_query_runs",
        ["tenant_id", "completed_at"],
    )

    op.create_table(
        "logistics_query_results",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("environment_serial", sa.String(length=64), nullable=False),
        sa.Column("environment_name", sa.String(length=255)),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("platform_order_no", sa.String(length=160)),
        sa.Column("order_time_text", sa.String(length=64)),
        sa.Column("amount_text", sa.String(length=64)),
        sa.Column("platform_status", sa.String(length=100)),
        sa.Column("status_label", sa.String(length=100)),
        sa.Column("fulfillment_stage", sa.String(length=100)),
        sa.Column("tracking_numbers", sa.JSON(), nullable=False),
        sa.Column("package_numbers", sa.JSON(), nullable=False),
        sa.Column("carrier", sa.String(length=100)),
        sa.Column("cancelled", sa.Boolean(), nullable=False),
        sa.Column("risk_order", sa.Boolean(), nullable=False),
        sa.Column("risk_summary", sa.String(length=300)),
        sa.Column("ip_address", sa.String(length=64)),
        sa.Column("time_zone", sa.String(length=100)),
        sa.Column("utc_offset_minutes", sa.Integer()),
        sa.Column("queried_at", sa.DateTime(timezone=True)),
        sa.Column("error_summary", sa.String(length=300)),
        sa.Column("screenshot_status", sa.String(length=32)),
        sa.Column("feishu_sync_status", sa.String(length=32), nullable=False),
        sa.Column("feishu_record_id", sa.String(length=128)),
        sa.Column("feishu_synced_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "status IN ('ok', 'fail', 'login', 'inuse', 'stopped', 'pending')",
            name="ck_logistics_result_status",
        ),
        sa.CheckConstraint(
            "feishu_sync_status IN ('pending', 'processing', 'completed', 'failed')",
            name="ck_logistics_result_feishu_status",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"], ["logistics_query_runs.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_id", "environment_serial", name="uq_logistics_result_run_env"),
    )
    op.create_index(
        "ix_logistics_result_tenant_created",
        "logistics_query_results",
        ["tenant_id", "created_at"],
    )
    op.create_index(
        "ix_logistics_result_order",
        "logistics_query_results",
        ["tenant_id", "platform_order_no"],
    )

    op.create_table(
        "operational_sync_outbox",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("aggregate_type", sa.String(length=64), nullable=False),
        sa.Column("aggregate_id", sa.Uuid(), nullable=False),
        sa.Column("dedupe_key", sa.String(length=255), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("external_record_id", sa.String(length=128)),
        sa.Column("last_error_code", sa.String(length=128)),
        sa.Column("processed_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "aggregate_type IN ('environment_creation_result', 'logistics_query_result')",
            name="ck_operational_sync_aggregate_type",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'processing', 'completed', 'failed')",
            name="ck_operational_sync_status",
        ),
        sa.CheckConstraint("attempt_count >= 0", name="ck_operational_sync_attempt_count"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("dedupe_key"),
    )
    op.create_index(
        "ix_operational_sync_pending",
        "operational_sync_outbox",
        ["status", "available_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_operational_sync_pending", table_name="operational_sync_outbox")
    op.drop_table("operational_sync_outbox")
    op.drop_index("ix_logistics_result_order", table_name="logistics_query_results")
    op.drop_index("ix_logistics_result_tenant_created", table_name="logistics_query_results")
    op.drop_table("logistics_query_results")
    op.drop_index("ix_logistics_run_tenant_completed", table_name="logistics_query_runs")
    op.drop_table("logistics_query_runs")
    op.drop_index(
        "ix_environment_result_environment_ref", table_name="environment_creation_results"
    )
    op.drop_index(
        "ix_environment_result_tenant_created", table_name="environment_creation_results"
    )
    op.drop_table("environment_creation_results")
    op.drop_index(
        "ix_environment_run_tenant_completed", table_name="environment_creation_runs"
    )
    op.drop_table("environment_creation_runs")
