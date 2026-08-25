"""Business operation logs P0.

Revision ID: 0005_business_operation_logs_p0
Revises: 0006_purchase_operator
Create Date: 2026-08-26
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "0005_business_operation_logs_p0"
down_revision: str | None = "0006_purchase_operator"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("audit_events", sa.Column("actor_name", sa.String(length=255)))
    op.add_column(
        "audit_events",
        sa.Column(
            "actor_roles",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'[]'::json"),
        ),
    )
    op.add_column(
        "audit_events",
        sa.Column(
            "category",
            sa.String(length=32),
            nullable=False,
            server_default="business_operation",
        ),
    )
    op.add_column(
        "audit_events",
        sa.Column(
            "module", sa.String(length=64), nullable=False, server_default="system"
        ),
    )
    op.add_column(
        "audit_events",
        sa.Column(
            "operation_type",
            sa.String(length=160),
            nullable=False,
            server_default="system.unknown",
        ),
    )
    op.add_column(
        "audit_events",
        sa.Column(
            "outcome", sa.String(length=32), nullable=False, server_default="success"
        ),
    )
    op.add_column("audit_events", sa.Column("business_object_type", sa.String(length=64)))
    op.add_column("audit_events", sa.Column("business_object_id", sa.String(length=160)))
    op.add_column("audit_events", sa.Column("business_object_no", sa.String(length=255)))
    op.add_column("audit_events", sa.Column("failure_reason", sa.String(length=160)))
    op.add_column(
        "audit_events",
        sa.Column(
            "change_summary",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'{}'::json"),
        ),
    )
    op.add_column(
        "audit_events",
        sa.Column("source", sa.String(length=64), nullable=False, server_default="api"),
    )
    op.add_column("audit_events", sa.Column("client_version", sa.String(length=64)))
    op.add_column(
        "audit_events",
        sa.Column(
            "trace_id", sa.String(length=64), nullable=False, server_default="migration"
        ),
    )

    op.execute(
        """
        UPDATE audit_events
        SET operation_type = CASE
                WHEN action = 'purchase_order.workspace.detail.read'
                    THEN 'purchase_order.recipient_sensitive.read'
                WHEN action = 'purchase_order.lines.claim'
                    THEN 'purchase_order.claim'
                WHEN action = 'purchase_order.split_plan.save'
                    THEN 'purchase_order.order_plan.modify'
                ELSE action
            END,
            trace_id = request_id,
            category = CASE
                WHEN action LIKE 'auth.%' THEN 'security'
                ELSE 'business_operation'
            END,
            module = CASE
                WHEN action LIKE 'purchase%' OR action LIKE 'procurement%' THEN 'procurement'
                WHEN action LIKE 'admin.%' THEN 'system'
                WHEN action LIKE 'auth.%' THEN 'auth'
                ELSE split_part(action, '.', 1)
            END,
            outcome = CASE
                WHEN result = 'success' THEN 'success'
                WHEN result = 'denied' AND (
                    details ->> 'permission' IS NOT NULL
                    OR details ->> 'reason' = 'permission_denied'
                ) THEN 'permission_denied'
                WHEN result = 'denied' AND (
                    details ->> 'reason' LIKE '%not_found%'
                    OR details ->> 'reason' LIKE '%cross_tenant%'
                ) THEN 'not_found'
                WHEN result = 'denied' AND (
                    details ->> 'reason' LIKE '%conflict%'
                    OR details ->> 'reason' LIKE '%locked%'
                ) THEN 'business_conflict'
                WHEN result = 'denied' THEN 'validation_failed'
                ELSE 'failure'
            END,
            business_object_id = COALESCE(
                details ->> 'purchaseOrderId',
                details ->> 'targetUserId',
                details ->> 'targetRoleId'
            ),
            failure_reason = NULLIF(details ->> 'reason', '')
        """
    )
    op.execute(
        """
        UPDATE audit_events AS event
        SET actor_name = app_user.display_name
        FROM users AS app_user
        WHERE event.actor_user_id = app_user.id
        """
    )
    op.execute(
        """
        UPDATE audit_events AS event
        SET actor_roles = role_snapshot.roles
        FROM (
            SELECT
                user_roles.user_id,
                json_agg(roles.code ORDER BY roles.code) AS roles
            FROM user_roles
            JOIN roles ON roles.id = user_roles.role_id
            GROUP BY user_roles.user_id
        ) AS role_snapshot
        WHERE event.actor_user_id = role_snapshot.user_id
        """
    )

    op.alter_column("audit_events", "actor_roles", server_default=None)
    op.alter_column("audit_events", "category", server_default=None)
    op.alter_column("audit_events", "module", server_default=None)
    op.alter_column("audit_events", "operation_type", server_default=None)
    op.alter_column("audit_events", "outcome", server_default=None)
    op.alter_column("audit_events", "change_summary", server_default=None)
    op.alter_column("audit_events", "source", server_default=None)
    op.alter_column("audit_events", "trace_id", server_default=None)

    op.create_index(
        "ix_audit_tenant_category_created",
        "audit_events",
        ["tenant_id", "category", "created_at"],
    )
    op.create_index(
        "ix_audit_tenant_actor_created",
        "audit_events",
        ["tenant_id", "actor_user_id", "created_at"],
    )
    op.create_index(
        "ix_audit_tenant_module_created",
        "audit_events",
        ["tenant_id", "module", "created_at"],
    )
    op.create_index(
        "ix_audit_tenant_business_no",
        "audit_events",
        ["tenant_id", "business_object_no"],
    )
    op.create_index(
        "ix_audit_tenant_operation",
        "audit_events",
        ["tenant_id", "operation_type"],
    )
    op.create_index(
        "ix_audit_tenant_request",
        "audit_events",
        ["tenant_id", "request_id"],
    )
    op.create_index(
        "ix_audit_tenant_trace",
        "audit_events",
        ["tenant_id", "trace_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_audit_tenant_trace", table_name="audit_events")
    op.drop_index("ix_audit_tenant_request", table_name="audit_events")
    op.drop_index("ix_audit_tenant_operation", table_name="audit_events")
    op.drop_index("ix_audit_tenant_business_no", table_name="audit_events")
    op.drop_index("ix_audit_tenant_module_created", table_name="audit_events")
    op.drop_index("ix_audit_tenant_actor_created", table_name="audit_events")
    op.drop_index("ix_audit_tenant_category_created", table_name="audit_events")
    for column in (
        "trace_id",
        "client_version",
        "source",
        "change_summary",
        "failure_reason",
        "business_object_no",
        "business_object_id",
        "business_object_type",
        "outcome",
        "operation_type",
        "module",
        "category",
        "actor_roles",
        "actor_name",
    ):
        op.drop_column("audit_events", column)
