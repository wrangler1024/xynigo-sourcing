"""Preserve all accepted package refunds for an order."""
from alembic import op
import sqlalchemy as sa
revision = "0040_after_sale_refunds"
down_revision = "0039_tracking_timestamp_defaults"
branch_labels = None
depends_on = None

def upgrade():
    op.add_column("after_sale_claim_results", sa.Column("refunds", sa.JSON(), nullable=False, server_default="[]"))

def downgrade():
    op.drop_column("after_sale_claim_results", "refunds")
