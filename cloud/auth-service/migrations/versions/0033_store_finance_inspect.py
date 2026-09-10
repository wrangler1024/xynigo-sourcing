"""Store finance inspection runs and per-store settlement snapshots."""

import sqlalchemy as sa
from alembic import op

revision = "0033_store_finance_inspect"
down_revision = "0032_procurement_import_history"
branch_labels = None
depends_on = None

RUN_STATUS_CHECK = (
    "status IN ('created', 'queued', 'leased', 'running', "
    "'completed', 'partial_failure', 'failed', 'cancelled', 'uncertain')"
)
RESULT_STATUS_CHECK = (
    "status IN ('ok', 'fail', 'login', 'inuse', 'stopped', "
    "'queued', 'running')"
)
TASK_TYPE_CHECK = (
    "task_type IN ('config.read.v1', 'config.write.v1', "
    "'workspace.rpc.v1', 'workspace.snapshot.v1', "
    "'environment.parse.v1', 'logistics.query.v1', "
    "'store.finance.inspect.v1', "
    "'environment.preview-bound.v1', "
    "'environment.create-bound.v1', 'environment.create-backup.v1', "
    "'environment.retry-row.v1', 'environment.retry-failed.v1')"
)


def upgrade():
    op.create_table(
        "store_finance_inspect_runs",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("tenant_id", sa.Uuid(),
                  sa.ForeignKey("tenants.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("actor_user_id", sa.Uuid(),
                  sa.ForeignKey("users.id", ondelete="RESTRICT"),
                  nullable=False),
        sa.Column("source_run_key", sa.String(128), nullable=False),
        sa.Column("payload_hash", sa.String(64), nullable=False),
        sa.Column("result_payload_hash", sa.String(64)),
        sa.Column("executor_id", sa.Uuid(),
                  sa.ForeignKey("local_executors.id", ondelete="SET NULL")),
        sa.Column("executor_task_id", sa.Uuid(),
                  sa.ForeignKey("executor_tasks.id", ondelete="SET NULL")),
        sa.Column("query_mode", sa.String(32), nullable=False),
        sa.Column("browser_mode", sa.String(20), nullable=False,
                  server_default="headless"),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("phase", sa.String(64), nullable=False,
                  server_default="created"),
        sa.Column("progress_completed", sa.Integer(), nullable=False),
        sa.Column("progress_total", sa.Integer(), nullable=False),
        sa.Column("stop_requested", sa.Boolean(), nullable=False),
        sa.Column("total_count", sa.Integer(), nullable=False),
        sa.Column("success_count", sa.Integer(), nullable=False),
        sa.Column("failed_count", sa.Integer(), nullable=False),
        sa.Column("request_summary", sa.JSON(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("source", sa.String(64), nullable=False),
        sa.Column("client_version", sa.String(64)),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now()),
        sa.CheckConstraint("query_mode IN ('initial', 'failed_retry')",
                           name="ck_store_finance_run_mode"),
        sa.CheckConstraint("browser_mode IN ('headless', 'visible')",
                           name="ck_store_finance_run_browser"),
        sa.CheckConstraint(RUN_STATUS_CHECK,
                           name="ck_store_finance_run_status"),
        sa.CheckConstraint(
            "total_count >= 0 AND success_count >= 0 AND failed_count >= 0 "
            "AND progress_completed >= 0 AND progress_total >= 0 "
            "AND progress_completed <= progress_total",
            name="ck_store_finance_run_counts"),
        sa.UniqueConstraint("tenant_id", "source_run_key",
                            name="uq_store_finance_run_tenant_source"),
    )
    op.create_index("ix_store_finance_run_tenant_status",
                    "store_finance_inspect_runs",
                    ["tenant_id", "status", "updated_at"])
    op.create_index("ix_store_finance_run_tenant_completed",
                    "store_finance_inspect_runs",
                    ["tenant_id", "completed_at"])
    op.create_index("ix_store_finance_run_executor_task",
                    "store_finance_inspect_runs", ["executor_task_id"],
                    unique=True)

    op.create_table(
        "store_finance_inspect_results",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("run_id", sa.Uuid(),
                  sa.ForeignKey("store_finance_inspect_runs.id",
                                ondelete="CASCADE"), nullable=False),
        sa.Column("tenant_id", sa.Uuid(),
                  sa.ForeignKey("tenants.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("environment_serial", sa.String(64), nullable=False),
        sa.Column("store_name", sa.String(128)),
        sa.Column("gs_code", sa.String(64)),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("login_mode", sa.String(32)),
        sa.Column("in_transit_amount", sa.Numeric(14, 2)),
        sa.Column("unsettled_amount", sa.Numeric(14, 2)),
        sa.Column("next_settlement_amount", sa.Numeric(14, 2)),
        sa.Column("next_settlement_date", sa.Date()),
        sa.Column("completed_settlement_amount", sa.Numeric(14, 2)),
        sa.Column("non_withdrawable_amount", sa.Numeric(14, 2)),
        sa.Column("pending_settle_limit_amount", sa.Numeric(14, 2)),
        sa.Column("last_payout_amount", sa.Numeric(14, 2)),
        sa.Column("withdrawable_amount", sa.Numeric(14, 2)),
        sa.Column("collected_at", sa.DateTime(timezone=True)),
        sa.Column("duration_seconds", sa.Integer()),
        sa.Column("error_summary", sa.Text()),
        sa.Column("screenshot_content", sa.LargeBinary()),
        sa.Column("screenshot_sha256", sa.String(64)),
        sa.Column("screenshot_expires_at", sa.DateTime(timezone=True)),
        sa.Column("execution_version", sa.String(64)),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now()),
        sa.CheckConstraint(RESULT_STATUS_CHECK,
                           name="ck_store_finance_result_status"),
        sa.UniqueConstraint("run_id", "environment_serial",
                            name="uq_store_finance_result_run_serial"),
    )
    op.create_index("ix_store_finance_result_run_status",
                    "store_finance_inspect_results", ["run_id", "status"])

    op.drop_constraint("ck_executor_task_type", "executor_tasks",
                       type_="check")
    op.create_check_constraint("ck_executor_task_type", "executor_tasks",
                               TASK_TYPE_CHECK)


def downgrade():
    op.drop_constraint("ck_executor_task_type", "executor_tasks",
                       type_="check")
    op.create_check_constraint(
        "ck_executor_task_type", "executor_tasks",
        "task_type IN ('config.read.v1', 'config.write.v1', "
        "'workspace.rpc.v1', 'workspace.snapshot.v1', "
        "'environment.parse.v1', 'logistics.query.v1', "
        "'environment.preview-bound.v1', "
        "'environment.create-bound.v1', 'environment.create-backup.v1', "
        "'environment.retry-row.v1', 'environment.retry-failed.v1')",
    )
    op.drop_index("ix_store_finance_result_run_status",
                  "store_finance_inspect_results")
    op.drop_table("store_finance_inspect_results")
    op.drop_index("ix_store_finance_run_executor_task",
                  "store_finance_inspect_runs")
    op.drop_index("ix_store_finance_run_tenant_completed",
                  "store_finance_inspect_runs")
    op.drop_index("ix_store_finance_run_tenant_status",
                  "store_finance_inspect_runs")
    op.drop_table("store_finance_inspect_runs")
