"""Allow store.finance.lookup.v1 executor task type."""

import sqlalchemy as sa
from alembic import op

revision = "0034_store_finance_lookup"
down_revision = "0033_store_finance_inspect"
branch_labels = None
depends_on = None

NEW_CHECK = (
    "task_type IN ('config.read.v1', 'config.write.v1', "
    "'workspace.rpc.v1', 'workspace.snapshot.v1', "
    "'environment.parse.v1', 'logistics.query.v1', "
    "'store.finance.inspect.v1', 'store.finance.lookup.v1', "
    "'environment.preview-bound.v1', "
    "'environment.create-bound.v1', 'environment.create-backup.v1', "
    "'environment.retry-row.v1', 'environment.retry-failed.v1')"
)
OLD_CHECK = (
    "task_type IN ('config.read.v1', 'config.write.v1', "
    "'workspace.rpc.v1', 'workspace.snapshot.v1', "
    "'environment.parse.v1', 'logistics.query.v1', "
    "'store.finance.inspect.v1', "
    "'environment.preview-bound.v1', "
    "'environment.create-bound.v1', 'environment.create-backup.v1', "
    "'environment.retry-row.v1', 'environment.retry-failed.v1')"
)


def upgrade():
    op.drop_constraint("ck_executor_task_type", "executor_tasks",
                       type_="check")
    op.create_check_constraint("ck_executor_task_type", "executor_tasks",
                               NEW_CHECK)


def downgrade():
    op.drop_constraint("ck_executor_task_type", "executor_tasks",
                       type_="check")
    op.create_check_constraint("ck_executor_task_type", "executor_tasks",
                               OLD_CHECK)
