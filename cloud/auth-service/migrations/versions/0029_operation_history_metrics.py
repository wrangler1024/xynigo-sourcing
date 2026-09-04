"""Add stable environment history ids and logistics execution metrics.

Revision ID: 0029_operation_history_metrics
Revises: 0028_hub_environment_cache
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision: str = "0029_operation_history_metrics"
down_revision: str | None = "0028_hub_environment_cache"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "environment_creation_runs",
        sa.Column("root_run_id", sa.Uuid()),
    )
    op.create_foreign_key(
        "fk_environment_run_root",
        "environment_creation_runs",
        "environment_creation_runs",
        ["root_run_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.execute(sa.text("""
        WITH RECURSIVE lineage AS (
            SELECT id, id AS root_id
            FROM environment_creation_runs
            WHERE parent_run_id IS NULL
            UNION ALL
            SELECT child.id, lineage.root_id
            FROM environment_creation_runs AS child
            JOIN lineage ON child.parent_run_id = lineage.id
        )
        UPDATE environment_creation_runs AS run
        SET root_run_id = lineage.root_id
        FROM lineage
        WHERE run.id = lineage.id
    """))
    op.execute(sa.text("""
        UPDATE environment_creation_runs
        SET root_run_id = id
        WHERE root_run_id IS NULL
    """))
    op.create_index(
        "ix_environment_run_history_root",
        "environment_creation_runs",
        ["tenant_id", "actor_user_id", "root_run_id", "updated_at", "id"],
    )
    op.drop_constraint(
        "ck_environment_run_mode", "environment_creation_runs", type_="check"
    )
    op.create_check_constraint(
        "ck_environment_run_mode",
        "environment_creation_runs",
        "run_mode IN ('bound', 'backup', 'test', 'dry_run', "
        "'retry_row', 'retry_failed')",
    )

    op.add_column(
        "logistics_query_results",
        sa.Column(
            "execution_attempted",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.add_column(
        "logistics_query_results",
        sa.Column(
            "execution_duration_ms",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )
    op.create_check_constraint(
        "ck_logistics_result_execution_duration",
        "logistics_query_results",
        "execution_duration_ms >= 0",
    )
    op.create_index(
        "ix_logistics_run_history_updated",
        "logistics_query_runs",
        ["tenant_id", "actor_user_id", "root_run_id", "updated_at", "id"],
    )
    op.alter_column(
        "logistics_query_results", "execution_attempted", server_default=None
    )
    op.alter_column(
        "logistics_query_results", "execution_duration_ms", server_default=None
    )


def downgrade() -> None:
    op.drop_index(
        "ix_logistics_run_history_updated", table_name="logistics_query_runs"
    )
    op.drop_constraint(
        "ck_logistics_result_execution_duration",
        "logistics_query_results",
        type_="check",
    )
    op.drop_column("logistics_query_results", "execution_duration_ms")
    op.drop_column("logistics_query_results", "execution_attempted")

    op.drop_constraint(
        "ck_environment_run_mode", "environment_creation_runs", type_="check"
    )
    op.create_check_constraint(
        "ck_environment_run_mode",
        "environment_creation_runs",
        "run_mode IN ('bound', 'backup', 'test', 'retry_row', 'retry_failed')",
    )
    op.drop_index(
        "ix_environment_run_history_root", table_name="environment_creation_runs"
    )
    op.drop_constraint(
        "fk_environment_run_root", "environment_creation_runs", type_="foreignkey"
    )
    op.drop_column("environment_creation_runs", "root_run_id")
