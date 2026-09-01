"""Add a persistent per-account barrier for environment Runs.

Revision ID: 0021_env_run_guard
Revises: 0020_env_run_cleanup
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from alembic import op


revision: str = "0021_env_run_guard"
down_revision: str | None = "0020_env_run_cleanup"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "environment_account_run_guards",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("account_ref", sa.String(length=128), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column(
            "state", sa.String(length=32), nullable=False,
            server_default="active",
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "state IN ('active', 'cleanup_pending', 'cleanup_failed')",
            name="ck_environment_account_run_guard_state",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"], ["environment_creation_runs.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id", "account_ref",
            name="uq_environment_account_run_guard",
        ),
    )
    op.create_index(
        "ix_environment_account_run_guard_run",
        "environment_account_run_guards",
        ["run_id", "state"],
    )
    op.alter_column(
        "environment_account_run_guards", "state",
        server_default=None,
    )
    # Preserve fail-closed cleanup facts if 0020 was already live before this
    # barrier table is introduced.  Keep the newest failed cleanup per tenant
    # and account; identifiers are generated in Python so no PostgreSQL UUID
    # extension is required.
    bind = op.get_bind()
    failed_rows = bind.execute(sa.text("""
        SELECT DISTINCT ON (tenant_id, account_ref)
            tenant_id, account_ref, run_id
        FROM environment_creation_results
        WHERE created_in_run IS TRUE
          AND cleanup_status = 'failed'
        ORDER BY tenant_id, account_ref, updated_at DESC, id DESC
    """)).mappings().all()
    if failed_rows:
        guard_table = sa.table(
            "environment_account_run_guards",
            sa.column("id", sa.Uuid()),
            sa.column("tenant_id", sa.Uuid()),
            sa.column("account_ref", sa.String(length=128)),
            sa.column("run_id", sa.Uuid()),
            sa.column("state", sa.String(length=32)),
        )
        op.bulk_insert(guard_table, [
            {
                "id": uuid.uuid4(),
                "tenant_id": row["tenant_id"],
                "account_ref": row["account_ref"],
                "run_id": row["run_id"],
                "state": "cleanup_failed",
            }
            for row in failed_rows
        ])


def downgrade() -> None:
    op.drop_index(
        "ix_environment_account_run_guard_run",
        table_name="environment_account_run_guards",
    )
    op.drop_table("environment_account_run_guards")
