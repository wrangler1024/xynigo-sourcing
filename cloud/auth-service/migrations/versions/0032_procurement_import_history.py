"""Add procurement import history listing index.

Revision ID: 0032_procurement_import_history
Revises: 0031_data_source_registry_sync
"""

from __future__ import annotations

from alembic import op


revision: str = "0032_procurement_import_history"
down_revision: str | None = "0031_data_source_registry_sync"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ix_procurement_import_job_tenant_created",
        "procurement_import_jobs",
        ["tenant_id", "created_at", "id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_procurement_import_job_tenant_created",
        table_name="procurement_import_jobs",
    )
