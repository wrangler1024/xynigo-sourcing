"""Retain current-node evidence and the last successful tracking note."""
from alembic import op
import sqlalchemy as sa

revision = "0043_after_sale_phase_evidence"
down_revision = "0042_after_sale_result_facts"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("after_sale_refund_tracking", sa.Column("phase_evidence", sa.JSON(), nullable=True))
    op.add_column("after_sale_refund_tracking", sa.Column("note", sa.Text(), nullable=True))


def downgrade():
    op.drop_column("after_sale_refund_tracking", "note")
    op.drop_column("after_sale_refund_tracking", "phase_evidence")
