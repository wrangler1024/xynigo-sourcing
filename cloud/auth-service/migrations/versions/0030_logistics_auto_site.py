"""Allow per-environment routing for logistics queries."""

from alembic import op

revision = "0030_logistics_auto_site"
down_revision = "0029_operation_history_metrics"
branch_labels = None
depends_on = None


def upgrade():
    op.drop_constraint("ck_logistics_run_site", "logistics_query_runs", type_="check")
    op.create_check_constraint(
        "ck_logistics_run_site", "logistics_query_runs",
        "site IN ('US', 'MX', 'AUTO')",
    )


def downgrade():
    # Never relabel a mixed-country run as MX/US to force a downgrade.
    op.drop_constraint("ck_logistics_run_site", "logistics_query_runs", type_="check")
    op.create_check_constraint(
        "ck_logistics_run_site", "logistics_query_runs", "site IN ('US', 'MX')",
    )
