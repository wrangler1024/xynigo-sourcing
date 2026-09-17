"""财务中心 · 结算看板（SHEIN 开放平台纯接口方案）建表。

口径依据 docs/20260917_需求_财务中心结算看板.md（20260917 真机实测 + 拍板）：
- 在途 = 订单级 estimatedGrossIncome(orderStatus=4)；订单列表窗口 ≤48h，
  直接查会漏单 → 必须有本地订单台账。
- 待结算 / 下次结算 = 对账单 checkStatus=1 收支轧差；金额天然按 estimatePayTime
  分批，所以单独建批次表而不是压成快照上的一个数。
- 已结算 = 报账单 reportStatus=2 历史全量累计。
- 人民币折算的汇率随快照留存（fx_rate_snapshot），否则回看历史时用今天的
  汇率重算，历史值每次都在变。
"""

import sqlalchemy as sa
from alembic import op

revision = "0045_shein_settlement"
down_revision = "0044_shein_store_auth"
branch_labels = None
depends_on = None


def upgrade():
    # 先建运行记录表：快照表有指向它的外键。
    op.create_table(
        "shein_settlement_sync_runs",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("tenant_id", sa.Uuid(),
                  sa.ForeignKey("tenants.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("trigger", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("store_total", sa.Integer(), nullable=False,
                  server_default="0"),
        sa.Column("store_ok", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("store_failed", sa.Integer(), nullable=False,
                  server_default="0"),
        sa.Column("detail", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("created_by_user_id", sa.Uuid(),
                  sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.CheckConstraint("trigger IN ('manual', 'schedule')",
                           name="ck_shein_settle_run_trigger"),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'succeeded', 'partial', 'failed')",
            name="ck_shein_settle_run_status",
        ),
    )
    op.create_index("ix_shein_settle_run_tenant_status",
                    "shein_settlement_sync_runs", ["tenant_id", "status"])

    op.create_table(
        "shein_settlement_orders",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("tenant_id", sa.Uuid(),
                  sa.ForeignKey("tenants.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("store_id", sa.Uuid(),
                  sa.ForeignKey("shein_authorized_stores.id",
                                ondelete="CASCADE"),
                  nullable=False),
        sa.Column("order_no", sa.String(64), nullable=False),
        sa.Column("order_status", sa.Integer(), nullable=False),
        sa.Column("currency", sa.String(8), nullable=False),
        sa.Column("estimated_gross_income", sa.Numeric(18, 4)),
        sa.Column("order_time", sa.DateTime(timezone=True)),
        sa.Column("update_time", sa.DateTime(timezone=True)),
        sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("tenant_id", "store_id", "order_no",
                            name="uq_shein_settle_order"),
    )
    op.create_index("ix_shein_settle_order_store_status",
                    "shein_settlement_orders",
                    ["tenant_id", "store_id", "order_status"])

    op.create_table(
        "shein_settlement_snapshots",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("tenant_id", sa.Uuid(),
                  sa.ForeignKey("tenants.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("store_id", sa.Uuid(),
                  sa.ForeignKey("shein_authorized_stores.id",
                                ondelete="CASCADE"),
                  nullable=False),
        sa.Column("sync_run_id", sa.Uuid(),
                  sa.ForeignKey("shein_settlement_sync_runs.id",
                                ondelete="SET NULL")),
        sa.Column("snapshot_date", sa.Date(), nullable=False),
        sa.Column("synced_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("currency", sa.String(8), nullable=False),
        sa.Column("in_transit_amount", sa.Numeric(18, 4)),
        sa.Column("unsettled_amount", sa.Numeric(18, 4)),
        sa.Column("settled_cumulative_amount", sa.Numeric(18, 4)),
        sa.Column("nearest_pay_date", sa.Date()),
        sa.Column("nearest_pay_amount", sa.Numeric(18, 4)),
        # 1 直接打款 / 2 钱包充值：钱包充值店的「已结算」只代表钱进平台钱包、
        # 未进公司账户，资金可用性口径不同，故随快照留存。
        sa.Column("payment_method", sa.Integer()),
        sa.Column("receiver_account", sa.String(128), nullable=False,
                  server_default=""),
        sa.Column("fx_rate_snapshot", sa.JSON(), nullable=False,
                  server_default="{}"),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("error_summary", sa.String(300), nullable=False,
                  server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.CheckConstraint("status IN ('ok', 'fail')",
                           name="ck_shein_settle_snap_status"),
    )
    op.create_index("ix_shein_settle_snap_store_date",
                    "shein_settlement_snapshots",
                    ["tenant_id", "store_id", "snapshot_date"])

    op.create_table(
        "shein_payout_batches",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("tenant_id", sa.Uuid(),
                  sa.ForeignKey("tenants.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("store_id", sa.Uuid(),
                  sa.ForeignKey("shein_authorized_stores.id",
                                ondelete="CASCADE"),
                  nullable=False),
        sa.Column("pay_date", sa.Date(), nullable=False),
        sa.Column("currency", sa.String(8), nullable=False),
        sa.Column("amount", sa.Numeric(18, 4), nullable=False),
        sa.Column("synced_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("tenant_id", "store_id", "pay_date", "currency",
                            name="uq_shein_payout_batch"),
    )
    op.create_index("ix_shein_payout_batch_date", "shein_payout_batches",
                    ["tenant_id", "pay_date"])

    op.create_table(
        "shein_fx_rates",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("tenant_id", sa.Uuid(),
                  sa.ForeignKey("tenants.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("currency", sa.String(8), nullable=False),
        sa.Column("rate", sa.Numeric(18, 8), nullable=False),
        sa.Column("effective_date", sa.Date(), nullable=False),
        sa.Column("source", sa.String(32), nullable=False,
                  server_default="manual"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.UniqueConstraint("tenant_id", "currency", "effective_date",
                            name="uq_shein_fx_rate"),
    )


def downgrade():
    op.drop_table("shein_fx_rates")
    op.drop_table("shein_payout_batches")
    op.drop_index("ix_shein_settle_snap_store_date",
                  table_name="shein_settlement_snapshots")
    op.drop_table("shein_settlement_snapshots")
    op.drop_index("ix_shein_settle_order_store_status",
                  table_name="shein_settlement_orders")
    op.drop_table("shein_settlement_orders")
    op.drop_index("ix_shein_settle_run_tenant_status",
                  table_name="shein_settlement_sync_runs")
    op.drop_table("shein_settlement_sync_runs")
