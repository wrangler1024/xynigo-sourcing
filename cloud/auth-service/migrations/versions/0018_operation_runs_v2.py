"""Extend daily operation results into durable cloud business runs.

Revision ID: 0018_operation_runs_v2
Revises: 0017_executor_workspace_rpc
Create Date: 2026-09-01
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0018_operation_runs_v2"
down_revision: str | None = "0017_executor_workspace_rpc"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


RUN_STATUS_CHECK = (
    "status IN ('created', 'queued', 'leased', 'running', "
    "'completed', 'partial_failure', 'failed', 'cancelled', 'uncertain')"
)


def _extend_run_table(table: str, prefix: str) -> None:
    op.add_column(table, sa.Column("result_payload_hash", sa.String(64)))
    op.add_column(table, sa.Column("executor_id", sa.Uuid()))
    op.add_column(table, sa.Column("executor_task_id", sa.Uuid()))
    op.add_column(
        table,
        sa.Column("phase", sa.String(64), nullable=False, server_default="created"),
    )
    op.add_column(
        table,
        sa.Column("attempt", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        table,
        sa.Column("progress_completed", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        table,
        sa.Column("progress_total", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        table,
        sa.Column("stop_requested", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        table,
        sa.Column("request_summary", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
    )
    op.add_column(table, sa.Column("last_heartbeat_at", sa.DateTime(timezone=True)))
    op.add_column(
        table,
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.alter_column(table, "completed_at", existing_type=sa.DateTime(timezone=True), nullable=True)
    op.create_foreign_key(
        f"fk_{prefix}_executor", table, "local_executors", ["executor_id"], ["id"], ondelete="SET NULL"
    )
    op.create_foreign_key(
        f"fk_{prefix}_executor_task",
        table,
        "executor_tasks",
        ["executor_task_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(f"ix_{prefix}_tenant_status", table, ["tenant_id", "status", "updated_at"])
    op.create_index(f"ix_{prefix}_executor_task", table, ["executor_task_id"], unique=True)


def _extend_result_table(table: str, prefix: str) -> None:
    op.add_column(table, sa.Column("current_step", sa.String(64)))
    op.add_column(
        table,
        sa.Column("completed_steps", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
    )
    op.add_column(
        table,
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )


def upgrade() -> None:
    op.add_column(
        "local_executors",
        sa.Column(
            "workspace_snapshot",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
    )
    op.add_column(
        "local_executors",
        sa.Column("workspace_snapshot_revision", sa.String(64)),
    )
    op.add_column(
        "local_executors",
        sa.Column("workspace_snapshot_at", sa.DateTime(timezone=True)),
    )
    op.drop_constraint("ck_executor_task_type", "executor_tasks", type_="check")
    op.create_check_constraint(
        "ck_executor_task_type",
        "executor_tasks",
        "task_type IN ('config.read.v1', 'config.write.v1', 'workspace.rpc.v1', "
        "'workspace.snapshot.v1', 'environment.parse.v1', "
        "'logistics.query.v1', 'environment.create-bound.v1', "
        "'environment.create-backup.v1', 'environment.retry-row.v1', "
        "'environment.retry-failed.v1')",
    )
    _extend_run_table("environment_creation_runs", "environment_run")
    op.add_column(
        "environment_creation_runs",
        sa.Column("run_mode", sa.String(32), nullable=False, server_default="bound"),
    )
    op.add_column(
        "environment_creation_runs",
        sa.Column("parent_run_id", sa.Uuid()),
    )
    op.create_foreign_key(
        "fk_environment_run_parent",
        "environment_creation_runs",
        "environment_creation_runs",
        ["parent_run_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_environment_run_parent",
        "environment_creation_runs",
        ["parent_run_id", "created_at"],
    )
    op.drop_constraint("ck_environment_run_status", "environment_creation_runs", type_="check")
    op.drop_constraint("ck_environment_run_counts", "environment_creation_runs", type_="check")
    op.create_check_constraint("ck_environment_run_status", "environment_creation_runs", RUN_STATUS_CHECK)
    op.create_check_constraint(
        "ck_environment_run_mode",
        "environment_creation_runs",
        "run_mode IN ('bound', 'backup', 'test', 'retry_row', 'retry_failed')",
    )
    op.create_check_constraint(
        "ck_environment_run_counts",
        "environment_creation_runs",
        "total_count >= 0 AND success_count >= 0 AND failed_count >= 0 "
        "AND attempt >= 0 AND progress_completed >= 0 AND progress_total >= 0 "
        "AND progress_completed <= progress_total",
    )
    _extend_result_table("environment_creation_results", "environment_result")
    op.drop_constraint("ck_environment_result_status", "environment_creation_results", type_="check")
    op.create_check_constraint(
        "ck_environment_result_status",
        "environment_creation_results",
        "status IN ('queued', 'running', 'success', 'failed', 'stopped')",
    )

    _extend_run_table("logistics_query_runs", "logistics_run")
    op.drop_constraint("ck_logistics_run_status", "logistics_query_runs", type_="check")
    op.drop_constraint("ck_logistics_run_counts", "logistics_query_runs", type_="check")
    op.create_check_constraint("ck_logistics_run_status", "logistics_query_runs", RUN_STATUS_CHECK)
    op.create_check_constraint(
        "ck_logistics_run_counts",
        "logistics_query_runs",
        "total_count >= 0 AND success_count >= 0 AND failed_count >= 0 "
        "AND attempt >= 0 AND progress_completed >= 0 AND progress_total >= 0 "
        "AND progress_completed <= progress_total",
    )
    _extend_result_table("logistics_query_results", "logistics_result")
    op.drop_constraint("ck_logistics_result_status", "logistics_query_results", type_="check")
    op.create_check_constraint(
        "ck_logistics_result_status",
        "logistics_query_results",
        "status IN ('ok', 'fail', 'login', 'inuse', 'stopped', 'pending', 'running')",
    )


def _shrink_run_table(table: str, prefix: str) -> None:
    op.execute(
        sa.text(
            f"UPDATE {table} SET completed_at = COALESCE(completed_at, updated_at, created_at) "
            "WHERE completed_at IS NULL"
        )
    )
    op.alter_column(table, "completed_at", existing_type=sa.DateTime(timezone=True), nullable=False)
    op.drop_index(f"ix_{prefix}_executor_task", table_name=table)
    op.drop_index(f"ix_{prefix}_tenant_status", table_name=table)
    op.drop_constraint(f"fk_{prefix}_executor_task", table, type_="foreignkey")
    op.drop_constraint(f"fk_{prefix}_executor", table, type_="foreignkey")
    for column in (
        "updated_at",
        "last_heartbeat_at",
        "request_summary",
        "stop_requested",
        "progress_total",
        "progress_completed",
        "attempt",
        "phase",
        "executor_task_id",
        "executor_id",
        "result_payload_hash",
    ):
        op.drop_column(table, column)


def _shrink_result_table(table: str) -> None:
    op.drop_column(table, "updated_at")
    op.drop_column(table, "completed_steps")
    op.drop_column(table, "current_step")


def downgrade() -> None:
    op.execute(
        sa.text(
            "DELETE FROM executor_task_events WHERE task_id IN "
            "(SELECT id FROM executor_tasks WHERE task_type IN "
            "('workspace.snapshot.v1', 'environment.parse.v1', 'logistics.query.v1', "
            "'environment.create-bound.v1', 'environment.create-backup.v1', "
            "'environment.retry-row.v1', 'environment.retry-failed.v1'))"
        )
    )
    op.execute(
        sa.text(
            "DELETE FROM executor_tasks WHERE task_type IN "
            "('workspace.snapshot.v1', 'environment.parse.v1', 'logistics.query.v1', "
            "'environment.create-bound.v1', 'environment.create-backup.v1', "
            "'environment.retry-row.v1', 'environment.retry-failed.v1')"
        )
    )
    op.execute(
        sa.text(
            "DELETE FROM environment_creation_results WHERE status NOT IN ('success', 'failed')"
        )
    )
    op.execute(
        sa.text(
            "DELETE FROM environment_creation_runs WHERE status NOT IN ('completed', 'partial_failure', 'failed')"
        )
    )
    op.execute(
        sa.text(
            "DELETE FROM logistics_query_runs WHERE status NOT IN ('completed', 'partial_failure', 'failed')"
        )
    )

    op.execute(sa.text("UPDATE logistics_query_results SET status = 'pending' WHERE status = 'running'"))
    op.drop_constraint("ck_logistics_result_status", "logistics_query_results", type_="check")
    op.create_check_constraint(
        "ck_logistics_result_status",
        "logistics_query_results",
        "status IN ('ok', 'fail', 'login', 'inuse', 'stopped', 'pending')",
    )
    _shrink_result_table("logistics_query_results")
    op.drop_constraint("ck_logistics_run_counts", "logistics_query_runs", type_="check")
    op.drop_constraint("ck_logistics_run_status", "logistics_query_runs", type_="check")
    op.create_check_constraint(
        "ck_logistics_run_status",
        "logistics_query_runs",
        "status IN ('completed', 'partial_failure', 'failed')",
    )
    op.create_check_constraint(
        "ck_logistics_run_counts",
        "logistics_query_runs",
        "total_count >= 0 AND success_count >= 0 AND failed_count >= 0",
    )
    _shrink_run_table("logistics_query_runs", "logistics_run")

    op.drop_constraint("ck_environment_result_status", "environment_creation_results", type_="check")
    op.create_check_constraint(
        "ck_environment_result_status",
        "environment_creation_results",
        "status IN ('success', 'failed')",
    )
    _shrink_result_table("environment_creation_results")
    op.drop_constraint("ck_environment_run_counts", "environment_creation_runs", type_="check")
    op.drop_constraint("ck_environment_run_mode", "environment_creation_runs", type_="check")
    op.drop_constraint("ck_environment_run_status", "environment_creation_runs", type_="check")
    op.create_check_constraint(
        "ck_environment_run_status",
        "environment_creation_runs",
        "status IN ('completed', 'partial_failure', 'failed')",
    )
    op.create_check_constraint(
        "ck_environment_run_counts",
        "environment_creation_runs",
        "total_count >= 0 AND success_count >= 0 AND failed_count >= 0",
    )
    op.drop_index("ix_environment_run_parent", table_name="environment_creation_runs")
    op.drop_constraint(
        "fk_environment_run_parent", "environment_creation_runs", type_="foreignkey"
    )
    op.drop_column("environment_creation_runs", "parent_run_id")
    op.drop_column("environment_creation_runs", "run_mode")
    _shrink_run_table("environment_creation_runs", "environment_run")
    op.drop_constraint("ck_executor_task_type", "executor_tasks", type_="check")
    op.create_check_constraint(
        "ck_executor_task_type",
        "executor_tasks",
        "task_type IN ('config.read.v1', 'config.write.v1', 'workspace.rpc.v1')",
    )
    op.drop_column("local_executors", "workspace_snapshot_at")
    op.drop_column("local_executors", "workspace_snapshot_revision")
    op.drop_column("local_executors", "workspace_snapshot")
