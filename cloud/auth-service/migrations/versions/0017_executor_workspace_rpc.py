"""Allow cloud workspace RPC tasks on paired local executors.

Revision ID: 0017_executor_workspace_rpc
Revises: 0016_system_order_key_v1
Create Date: 2026-08-28
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0017_executor_workspace_rpc"
down_revision: str | None = "0016_system_order_key_v1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint("ck_executor_task_type", "executor_tasks", type_="check")
    op.create_check_constraint(
        "ck_executor_task_type",
        "executor_tasks",
        "task_type IN ('config.read.v1', 'config.write.v1', 'workspace.rpc.v1')",
    )


def downgrade() -> None:
    op.execute(
        sa.text(
            "DELETE FROM executor_task_events WHERE task_id IN "
            "(SELECT id FROM executor_tasks WHERE task_type = 'workspace.rpc.v1')"
        )
    )
    op.execute(
        sa.text("DELETE FROM executor_tasks WHERE task_type = 'workspace.rpc.v1'")
    )
    op.drop_constraint("ck_executor_task_type", "executor_tasks", type_="check")
    op.create_check_constraint(
        "ck_executor_task_type",
        "executor_tasks",
        "task_type IN ('config.read.v1', 'config.write.v1')",
    )
