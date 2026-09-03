"""Add encrypted environment dry-run executor task.

Revision ID: 0026_environment_dry_run
Revises: 0025_hub_environment_inventory
"""

from __future__ import annotations

from alembic import op


revision: str = "0026_environment_dry_run"
down_revision: str | None = "0025_hub_environment_inventory"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint(
        "ck_executor_task_type", "executor_tasks", type_="check"
    )
    op.create_check_constraint(
        "ck_executor_task_type",
        "executor_tasks",
        "task_type IN ('config.read.v1', 'config.write.v1', "
        "'workspace.rpc.v1', 'workspace.snapshot.v1', "
        "'environment.parse.v1', 'environment.preview-bound.v1', "
        "'logistics.query.v1', 'environment.create-bound.v1', "
        "'environment.create-backup.v1', 'environment.retry-row.v1', "
        "'environment.retry-failed.v1')",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_executor_task_type", "executor_tasks", type_="check"
    )
    op.create_check_constraint(
        "ck_executor_task_type",
        "executor_tasks",
        "task_type IN ('config.read.v1', 'config.write.v1', "
        "'workspace.rpc.v1', 'workspace.snapshot.v1', "
        "'environment.parse.v1', 'logistics.query.v1', "
        "'environment.create-bound.v1', 'environment.create-backup.v1', "
        "'environment.retry-row.v1', 'environment.retry-failed.v1')",
    )
