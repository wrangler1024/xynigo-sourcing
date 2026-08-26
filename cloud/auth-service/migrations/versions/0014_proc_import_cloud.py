"""Add durable encrypted cloud procurement-import plans and jobs.

Revision ID: 0014_proc_import_cloud
Revises: 0013_purchase_task_feishu_mirror
Create Date: 2026-08-26
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "0014_proc_import_cloud"
down_revision: str | None = "0013_purchase_task_feishu_mirror"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "procurement_import_plans",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("created_by_user_id", sa.Uuid(), nullable=False),
        sa.Column("filename", sa.String(length=255), nullable=False),
        sa.Column("import_batch", sa.String(length=128), nullable=False),
        sa.Column("payload_hash", sa.String(length=64), nullable=False),
        sa.Column("encrypted_payload", sa.LargeBinary(), nullable=True),
        sa.Column(
            "status", sa.String(length=32), server_default="parsed", nullable=False
        ),
        sa.Column("source_row_count", sa.Integer(), nullable=False),
        sa.Column("order_count", sa.Integer(), nullable=False),
        sa.Column("detail_count", sa.Integer(), nullable=False),
        sa.Column("image_count", sa.Integer(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "status IN ('parsed', 'validated', 'expired')",
            name="ck_procurement_import_plan_status",
        ),
        sa.CheckConstraint(
            "source_row_count >= 0 AND order_count >= 0 AND "
            "detail_count >= 0 AND image_count >= 0",
            name="ck_procurement_import_plan_counts",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"], ["users.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_procurement_import_plan_tenant_expiry",
        "procurement_import_plans",
        ["tenant_id", "expires_at"],
    )

    op.create_table(
        "procurement_import_jobs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("plan_id", sa.Uuid(), nullable=False),
        sa.Column("created_by_user_id", sa.Uuid(), nullable=False),
        sa.Column("target_key_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "state", sa.String(length=32), server_default="queued", nullable=False
        ),
        sa.Column("progress", sa.JSON(), nullable=False),
        sa.Column("last_error_code", sa.String(length=128)),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "state IN ('queued', 'validating', 'normalizing_headers', "
            "'formatting_headers', 'writing_rows', 'verifying_rows', "
            "'formatting_rows', 'writing_links', 'writing_images', "
            "'completed', 'partial', 'failed')",
            name="ck_procurement_import_job_state",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"], ["users.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["plan_id"], ["procurement_import_plans.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_procurement_import_job_pending",
        "procurement_import_jobs",
        ["state", "created_at"],
    )
    op.create_index(
        "ix_procurement_import_job_target",
        "procurement_import_jobs",
        ["tenant_id", "target_key_hash"],
    )
    op.create_index(
        "uq_procurement_import_job_active_target",
        "procurement_import_jobs",
        ["tenant_id", "target_key_hash"],
        unique=True,
        postgresql_where=sa.text(
            "state IN ('queued', 'validating', 'normalizing_headers', "
            "'formatting_headers', 'writing_rows', 'verifying_rows', "
            "'formatting_rows', 'writing_links', 'writing_images')"
        ),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_procurement_import_job_active_target",
        table_name="procurement_import_jobs",
    )
    op.drop_index(
        "ix_procurement_import_job_target", table_name="procurement_import_jobs"
    )
    op.drop_index(
        "ix_procurement_import_job_pending", table_name="procurement_import_jobs"
    )
    op.drop_table("procurement_import_jobs")
    op.drop_index(
        "ix_procurement_import_plan_tenant_expiry",
        table_name="procurement_import_plans",
    )
    op.drop_table("procurement_import_plans")
