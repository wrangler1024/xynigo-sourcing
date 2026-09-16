# -*- coding: utf-8 -*-
"""售后提交结果补三个展示字段：退款账户、送达时间、商品图。

理由是设计定版后结果表要展示「退款落到哪张卡」「什么时候送达」「买的是什么」，
其中退款账户来自提交落地页的退款账户区块（掩码），送达时间与商品图来自扫描阶段。
三个字段都可空——取不到时按 — 展示，不阻断提交。
"""
from alembic import op
import sqlalchemy as sa

revision = "0037_after_sale_claim_fields"
down_revision = "0036_after_sale_claim"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("after_sale_claim_results",
                  sa.Column("refund_account", sa.String(length=40), nullable=True))
    op.add_column("after_sale_claim_results",
                  sa.Column("delivered_at", sa.String(length=32), nullable=True))
    op.add_column("after_sale_claim_results",
                  sa.Column("goods_img", sa.String(length=300), nullable=True))


def downgrade() -> None:
    op.drop_column("after_sale_claim_results", "goods_img")
    op.drop_column("after_sale_claim_results", "delivered_at")
    op.drop_column("after_sale_claim_results", "refund_account")
