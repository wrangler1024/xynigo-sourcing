# -*- coding: utf-8 -*-
"""退款跟踪表：按 refund_bill_id 唯一，一个退款单一行。

与提交结果表的分工：提交结果＝我提交了什么（不可变）；本表＝平台后来怎么处理
（只读回访覆盖更新），因此 checked_at 会变、phase 会推进，到已退款/已拒绝冻结。
timeline 存平台时间轴原文（只跟随导出取用，不进进度快照）。
"""
from alembic import op
import sqlalchemy as sa

revision = "0037_after_sale_refund_tracking"
down_revision = "0036_after_sale_claim_fields"
branch_labels = None
depends_on = None

NEW_TASK_TYPE_CHECK = (
    "task_type IN ('config.read.v1', 'config.write.v1', "
    "'workspace.rpc.v1', 'workspace.snapshot.v1', "
    "'environment.parse.v1', 'logistics.query.v1', "
    "'store.finance.inspect.v1', 'store.finance.lookup.v1', "
    "'after.sale.scan.v1', 'after.sale.claim.v1', "
    "'after.sale.track.v1', "
    "'environment.preview-bound.v1', "
    "'environment.create-bound.v1', 'environment.create-backup.v1', "
    "'environment.retry-row.v1', 'environment.retry-failed.v1')"
)
OLD_TASK_TYPE_CHECK = (
    "task_type IN ('config.read.v1', 'config.write.v1', "
    "'workspace.rpc.v1', 'workspace.snapshot.v1', "
    "'environment.parse.v1', 'logistics.query.v1', "
    "'store.finance.inspect.v1', 'store.finance.lookup.v1', "
    "'after.sale.scan.v1', 'after.sale.claim.v1', "
    "'environment.preview-bound.v1', "
    "'environment.create-bound.v1', 'environment.create-backup.v1', "
    "'environment.retry-row.v1', 'environment.retry-failed.v1')"
)


def upgrade() -> None:
    op.create_table(
        "after_sale_refund_tracking",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("refund_bill_id", sa.String(length=32), nullable=False),
        sa.Column("order_no", sa.String(length=32), nullable=False),
        sa.Column("environment_serial", sa.String(length=64), nullable=True),
        sa.Column("store_name", sa.String(length=128), nullable=True),
        sa.Column("phase", sa.String(length=24), nullable=True),
        sa.Column("phase_label", sa.String(length=24), nullable=True),
        sa.Column("countdown", sa.String(length=24), nullable=True),
        sa.Column("refund_account", sa.String(length=40), nullable=True),
        sa.Column("amount", sa.String(length=24), nullable=True),
        sa.Column("timeline", sa.Text(), nullable=True),
        sa.Column("last_status", sa.String(length=16), nullable=True),
        sa.Column("last_error", sa.String(length=300), nullable=True),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"],
                                ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "refund_bill_id",
                            name="uq_after_sale_track_tenant_bill"),
    )
    op.create_index("ix_after_sale_track_tenant_phase",
                    "after_sale_refund_tracking", ["tenant_id", "phase"])
    op.create_index("ix_after_sale_track_tenant_checked",
                    "after_sale_refund_tracking", ["tenant_id", "checked_at"])
    op.drop_constraint("ck_executor_task_type", "executor_tasks",
                       type_="check")
    op.create_check_constraint("ck_executor_task_type", "executor_tasks",
                               NEW_TASK_TYPE_CHECK)


def downgrade() -> None:
    op.drop_constraint("ck_executor_task_type", "executor_tasks",
                       type_="check")
    op.create_check_constraint("ck_executor_task_type", "executor_tasks",
                               OLD_TASK_TYPE_CHECK)
    op.drop_index("ix_after_sale_track_tenant_checked",
                  table_name="after_sale_refund_tracking")
    op.drop_index("ix_after_sale_track_tenant_phase",
                  table_name="after_sale_refund_tracking")
    op.drop_table("after_sale_refund_tracking")
