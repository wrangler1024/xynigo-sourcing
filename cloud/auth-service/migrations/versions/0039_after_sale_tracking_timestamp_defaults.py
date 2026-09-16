# -*- coding: utf-8 -*-
"""给退款跟踪表的时间列补数据库默认值（修 0038 的漏写）。

0038 建表时 created_at/updated_at 写成 nullable=False 却**没有 server_default**。
模型侧（models.AfterSaleRefundTracking）声明了 server_default=func.now()，于是
SQLAlchemy 的 INSERT 直接省略这两列，落到库里就是 NULL → NOT NULL 违约：

    psycopg.errors.NotNullViolation: null value in column "created_at" of
    relation "after_sale_refund_tracking" violates not-null constraint

后果：④ 退款回访每轮上报都被 500 掉，而执行器侧 _safe_report 吞掉异常 → 任务
显示 succeeded、跟踪表却一行没有。测试用 models 元数据建表（server_default 生效）
所以本地全绿，只有真机才暴露。

修法：把两个列的库端默认值补上（与 0036 建表时一致）。SET DEFAULT 幂等，
已手工补过的库再跑一次也无害。
"""
from alembic import op
import sqlalchemy as sa

revision = "0039_after_sale_tracking_timestamp_defaults"
down_revision = "0038_after_sale_refund_tracking"
branch_labels = None
depends_on = None

TABLE = "after_sale_refund_tracking"


def upgrade() -> None:
    for column in ("created_at", "updated_at"):
        op.alter_column(
            TABLE, column,
            existing_type=sa.DateTime(timezone=True),
            existing_nullable=False,
            server_default=sa.func.now(),
        )


def downgrade() -> None:
    for column in ("created_at", "updated_at"):
        op.alter_column(
            TABLE, column,
            existing_type=sa.DateTime(timezone=True),
            existing_nullable=False,
            server_default=None,
        )
