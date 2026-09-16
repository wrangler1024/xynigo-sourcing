"""After-sale scan task type, claim runs and per-order submission results."""

import sqlalchemy as sa
from alembic import op

revision = "0036_after_sale_claim"
down_revision = "0035_purchase_receipts"
branch_labels = None
depends_on = None

RUN_STATUS_CHECK = (
    "status IN ('created', 'queued', 'leased', 'running', "
    "'completed', 'partial_failure', 'failed', 'cancelled', 'uncertain')"
)
RESULT_STATUS_CHECK = (
    "status IN ('ok', 'empty', 'skip', 'blocked', 'fail', 'login', "
    "'inuse', 'stopped', 'queued', 'running')"
)
# 与 models.ExecutorTask.__table_args__ 的 ck_executor_task_type 逐字一致：
# 新增 after.sale.scan.v1 / after.sale.claim.v1 两个业务任务类型。
NEW_TASK_TYPE_CHECK = (
    "task_type IN ('config.read.v1', 'config.write.v1', "
    "'workspace.rpc.v1', 'workspace.snapshot.v1', "
    "'environment.parse.v1', 'logistics.query.v1', "
    "'store.finance.inspect.v1', 'store.finance.lookup.v1', "
    "'after.sale.scan.v1', 'after.sale.claim.v1', "
    "'environment.preview-bound.v1', "
    "'environment.create-bound.v1', 'environment.create-backup.v1', "
    "'environment.retry-row.v1', 'environment.retry-failed.v1')"
)
OLD_TASK_TYPE_CHECK = (
    "task_type IN ('config.read.v1', 'config.write.v1', "
    "'workspace.rpc.v1', 'workspace.snapshot.v1', "
    "'environment.parse.v1', 'logistics.query.v1', "
    "'store.finance.inspect.v1', 'store.finance.lookup.v1', "
    "'environment.preview-bound.v1', "
    "'environment.create-bound.v1', 'environment.create-backup.v1', "
    "'environment.retry-row.v1', 'environment.retry-failed.v1')"
)


def upgrade():
    op.create_table(
        "after_sale_claim_runs",
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
        sa.Column("browser_mode", sa.String(20), nullable=False,
                  server_default="visible"),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("phase", sa.String(64), nullable=False,
                  server_default="created"),
        sa.Column("progress_completed", sa.Integer(), nullable=False),
        sa.Column("progress_total", sa.Integer(), nullable=False),
        sa.Column("stop_requested", sa.Boolean(), nullable=False),
        sa.Column("total_count", sa.Integer(), nullable=False),
        sa.Column("success_count", sa.Integer(), nullable=False),
        sa.Column("failed_count", sa.Integer(), nullable=False),
        sa.Column("skipped_count", sa.Integer(), nullable=False),
        sa.Column("stopped_count", sa.Integer(), nullable=False),
        sa.Column("request_summary", sa.JSON(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("source", sa.String(64), nullable=False),
        sa.Column("client_version", sa.String(64)),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now()),
        sa.CheckConstraint("browser_mode IN ('headless', 'visible')",
                           name="ck_after_sale_run_browser"),
        sa.CheckConstraint(RUN_STATUS_CHECK,
                           name="ck_after_sale_run_status"),
        sa.CheckConstraint(
            "total_count >= 0 AND success_count >= 0 AND failed_count >= 0 "
            "AND skipped_count >= 0 AND stopped_count >= 0 "
            "AND progress_completed >= 0 AND progress_total >= 0 "
            "AND progress_completed <= progress_total",
            name="ck_after_sale_run_counts"),
        sa.UniqueConstraint("tenant_id", "source_run_key",
                            name="uq_after_sale_run_tenant_source"),
    )
    op.create_index("ix_after_sale_run_tenant_status", "after_sale_claim_runs",
                    ["tenant_id", "status", "updated_at"])
    op.create_index("ix_after_sale_run_tenant_completed", "after_sale_claim_runs",
                    ["tenant_id", "completed_at"])
    op.create_index("ix_after_sale_run_executor_task", "after_sale_claim_runs",
                    ["executor_task_id"], unique=True)

    op.create_table(
        "after_sale_claim_results",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("run_id", sa.Uuid(),
                  sa.ForeignKey("after_sale_claim_runs.id",
                                ondelete="CASCADE"), nullable=False),
        sa.Column("tenant_id", sa.Uuid(),
                  sa.ForeignKey("tenants.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("order_no", sa.String(32), nullable=False),
        sa.Column("environment_serial", sa.String(64), nullable=False),
        sa.Column("store_name", sa.String(128)),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("package_no", sa.String(64)),
        sa.Column("refund_bill_id", sa.String(32)),
        sa.Column("refund_path", sa.String(48)),
        sa.Column("duration_seconds", sa.Integer()),
        sa.Column("submitted_at", sa.DateTime(timezone=True)),
        sa.Column("note", sa.Text()),
        sa.Column("error_summary", sa.Text()),
        sa.Column("screenshot_content", sa.LargeBinary()),
        sa.Column("screenshot_sha256", sa.String(64)),
        sa.Column("screenshot_expires_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now()),
        sa.CheckConstraint(RESULT_STATUS_CHECK,
                           name="ck_after_sale_result_status"),
        sa.CheckConstraint("duration_seconds IS NULL OR duration_seconds >= 0",
                           name="ck_after_sale_result_duration"),
        sa.UniqueConstraint("run_id", "order_no",
                            name="uq_after_sale_result_run_order"),
    )
    op.create_index("ix_after_sale_result_run_status", "after_sale_claim_results",
                    ["run_id", "status"])

    # after.sale.scan.v1 不建 Run：扫描进度快照直接挂在任务行上。
    op.add_column("executor_tasks",
                  sa.Column("progress_summary", sa.JSON(), nullable=True))

    op.drop_constraint("ck_executor_task_type", "executor_tasks",
                       type_="check")
    op.create_check_constraint("ck_executor_task_type", "executor_tasks",
                               NEW_TASK_TYPE_CHECK)


def downgrade():
    op.drop_constraint("ck_executor_task_type", "executor_tasks",
                       type_="check")
    op.create_check_constraint("ck_executor_task_type", "executor_tasks",
                               OLD_TASK_TYPE_CHECK)
    op.drop_column("executor_tasks", "progress_summary")
    op.drop_index("ix_after_sale_result_run_status", "after_sale_claim_results")
    op.drop_table("after_sale_claim_results")
    op.drop_index("ix_after_sale_run_executor_task", "after_sale_claim_runs")
    op.drop_index("ix_after_sale_run_tenant_completed", "after_sale_claim_runs")
    op.drop_index("ix_after_sale_run_tenant_status", "after_sale_claim_runs")
    op.drop_table("after_sale_claim_runs")
