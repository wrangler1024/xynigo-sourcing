"""Independent system runtime and error logs.

Revision ID: 0007_system_runtime_logs_p1
Revises: 0005_business_operation_logs_p0
Create Date: 2026-08-26
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "0007_system_runtime_logs_p1"
down_revision: str | None = "0005_business_operation_logs_p0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "system_log_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid()),
        sa.Column("actor_user_id", sa.Uuid()),
        sa.Column("actor_name", sa.String(length=255)),
        sa.Column("category", sa.String(length=32), nullable=False),
        sa.Column("level", sa.String(length=16), nullable=False),
        sa.Column("service", sa.String(length=64), nullable=False),
        sa.Column("component", sa.String(length=64), nullable=False),
        sa.Column("environment", sa.String(length=32), nullable=False),
        sa.Column("event_type", sa.String(length=160), nullable=False),
        sa.Column("message", sa.String(length=500), nullable=False),
        sa.Column("http_method", sa.String(length=16)),
        sa.Column("route", sa.String(length=255)),
        sa.Column("status_code", sa.Integer()),
        sa.Column("duration_ms", sa.Integer()),
        sa.Column("exception_type", sa.String(length=160)),
        sa.Column("error_code", sa.String(length=160)),
        sa.Column("fingerprint", sa.String(length=64), nullable=False),
        sa.Column("source", sa.String(length=64), nullable=False),
        sa.Column("client_version", sa.String(length=64)),
        sa.Column("request_id", sa.String(length=64), nullable=False),
        sa.Column("trace_id", sa.String(length=64), nullable=False),
        sa.Column("details", sa.JSON(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now()
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "category IN ('system_runtime', 'system_error')",
            name="ck_system_log_category",
        ),
        sa.CheckConstraint(
            "level IN ('info', 'warning', 'error', 'critical')",
            name="ck_system_log_level",
        ),
        sa.ForeignKeyConstraint(
            ["actor_user_id"], ["users.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_system_log_tenant_created",
        "system_log_events",
        ["tenant_id", "created_at"],
    )
    op.create_index(
        "ix_system_log_tenant_category_level_created",
        "system_log_events",
        ["tenant_id", "category", "level", "created_at"],
    )
    op.create_index(
        "ix_system_log_tenant_service_created",
        "system_log_events",
        ["tenant_id", "service", "created_at"],
    )
    op.create_index(
        "ix_system_log_tenant_event_type",
        "system_log_events",
        ["tenant_id", "event_type"],
    )
    op.create_index(
        "ix_system_log_tenant_status_created",
        "system_log_events",
        ["tenant_id", "status_code", "created_at"],
    )
    op.create_index(
        "ix_system_log_tenant_request",
        "system_log_events",
        ["tenant_id", "request_id"],
    )
    op.create_index(
        "ix_system_log_tenant_trace",
        "system_log_events",
        ["tenant_id", "trace_id"],
    )
    op.create_index(
        "ix_system_log_fingerprint_created",
        "system_log_events",
        ["fingerprint", "created_at"],
    )
    op.create_index("ix_system_log_expires", "system_log_events", ["expires_at"])


def downgrade() -> None:
    op.drop_index("ix_system_log_expires", table_name="system_log_events")
    op.drop_index("ix_system_log_fingerprint_created", table_name="system_log_events")
    op.drop_index("ix_system_log_tenant_trace", table_name="system_log_events")
    op.drop_index("ix_system_log_tenant_request", table_name="system_log_events")
    op.drop_index("ix_system_log_tenant_status_created", table_name="system_log_events")
    op.drop_index("ix_system_log_tenant_event_type", table_name="system_log_events")
    op.drop_index("ix_system_log_tenant_service_created", table_name="system_log_events")
    op.drop_index(
        "ix_system_log_tenant_category_level_created",
        table_name="system_log_events",
    )
    op.drop_index("ix_system_log_tenant_created", table_name="system_log_events")
    op.drop_table("system_log_events")
