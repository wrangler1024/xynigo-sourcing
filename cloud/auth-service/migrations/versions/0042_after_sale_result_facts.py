"""Keep order product facts and action completion separate from refund evidence."""
from alembic import op
import sqlalchemy as sa

revision = "0042_after_sale_result_facts"
down_revision = "0041_after_sale_uncertain"
branch_labels = None
depends_on = None


def upgrade():
    for name in ("goods_images", "goods_items"):
        op.add_column("after_sale_claim_results", sa.Column(name, sa.JSON(), nullable=False, server_default="[]"))
    op.add_column("after_sale_claim_results", sa.Column("item_count", sa.Integer(), nullable=True))
    op.add_column("after_sale_claim_results", sa.Column("operation_completed_at", sa.DateTime(timezone=True), nullable=True))
    for name in ("submission_error", "recovery_error"):
        op.add_column("after_sale_claim_results", sa.Column(name, sa.Text(), nullable=True))


def downgrade():
    for name in ("recovery_error", "submission_error", "operation_completed_at", "item_count", "goods_items", "goods_images"):
        op.drop_column("after_sale_claim_results", name)
