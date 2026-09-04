"""Add first tracking event and order-to-track lead time.

Revision ID: 0027_logistics_first_tracking
Revises: 0026_environment_dry_run
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision: str = "0027_logistics_first_tracking"
down_revision: str | None = "0026_environment_dry_run"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "logistics_query_results",
        sa.Column("first_tracking_at", sa.DateTime(timezone=True)),
    )
    op.add_column(
        "logistics_query_results",
        sa.Column("first_tracking_time_text", sa.String(length=64)),
    )
    op.add_column(
        "logistics_query_results",
        sa.Column("first_tracking_summary", sa.String(length=300)),
    )
    op.add_column(
        "logistics_query_results",
        sa.Column("first_tracking_lead_minutes", sa.Integer()),
    )
    op.create_check_constraint(
        "ck_logistics_first_tracking_lead_minutes",
        "logistics_query_results",
        "first_tracking_lead_minutes IS NULL OR "
        "(first_tracking_lead_minutes >= 0 AND "
        "first_tracking_lead_minutes <= 527040)",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_logistics_first_tracking_lead_minutes",
        "logistics_query_results",
        type_="check",
    )
    op.drop_column("logistics_query_results", "first_tracking_lead_minutes")
    op.drop_column("logistics_query_results", "first_tracking_summary")
    op.drop_column("logistics_query_results", "first_tracking_time_text")
    op.drop_column("logistics_query_results", "first_tracking_at")
