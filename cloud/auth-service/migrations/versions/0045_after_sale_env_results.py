"""Store per-environment outcomes for by-environment after-sale claim runs."""
from alembic import op
import sqlalchemy as sa

revision = "0045_after_sale_env_results"
# 并行线的 0044（SHEIN 店铺授权）已先合入 main，本迁移顺延为 0045 接在其后。
down_revision = "0044_shein_store_auth"
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
