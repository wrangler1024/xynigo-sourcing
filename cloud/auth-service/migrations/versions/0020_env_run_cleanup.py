"""Persist environment ownership, compensating cleanup and IP probe errors.

Revision ID: 0020_env_run_cleanup
Revises: 0019_environment_cloud_plan
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0020_env_run_cleanup"
down_revision: str | None = "0019_environment_cloud_plan"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "environment_creation_results",
        sa.Column(
            "created_in_run", sa.Boolean(), nullable=False,
            server_default=sa.false(),
        ),
    )
    op.add_column(
        "environment_creation_results",
        sa.Column(
            "cleanup_status", sa.String(length=32), nullable=False,
            server_default="not_required",
        ),
    )
    op.add_column(
        "environment_creation_results",
        sa.Column("cleanup_error_code", sa.String(length=128)),
    )
    op.add_column(
        "environment_creation_results",
        sa.Column("cleanup_error_summary", sa.String(length=300)),
    )
    op.add_column(
        "environment_creation_results",
        sa.Column("ip_error_code", sa.String(length=128)),
    )
    op.add_column(
        "environment_creation_results",
        sa.Column("ip_error_summary", sa.String(length=300)),
    )
    op.create_check_constraint(
        "ck_environment_result_cleanup_status",
        "environment_creation_results",
        "cleanup_status IN ('not_required', 'pending', 'deleting', "
        "'deleted', 'failed')",
    )
    op.alter_column(
        "environment_creation_results", "created_in_run",
        server_default=None,
    )
    op.alter_column(
        "environment_creation_results", "cleanup_status",
        server_default=None,
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_environment_result_cleanup_status",
        "environment_creation_results",
        type_="check",
    )
    op.drop_column("environment_creation_results", "ip_error_summary")
    op.drop_column("environment_creation_results", "ip_error_code")
    op.drop_column("environment_creation_results", "cleanup_error_summary")
    op.drop_column("environment_creation_results", "cleanup_error_code")
    op.drop_column("environment_creation_results", "cleanup_status")
    op.drop_column("environment_creation_results", "created_in_run")
