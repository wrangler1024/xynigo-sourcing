"""Store per-environment outcomes for by-environment after-sale claim runs."""
from alembic import op
import sqlalchemy as sa

revision = "0044_after_sale_env_results"
down_revision = "0043_after_sale_phase_evidence"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "after_sale_claim_runs",
        sa.Column("environment_results", sa.JSON(), nullable=False,
                  server_default="[]"),
    )


def downgrade():
    op.drop_column("after_sale_claim_runs", "environment_results")
