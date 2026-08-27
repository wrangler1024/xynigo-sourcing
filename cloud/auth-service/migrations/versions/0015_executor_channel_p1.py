"""Add local executor pairing, device channel, task leases and events.

Revision ID: 0015_executor_channel_p1
Revises: 0014_proc_import_cloud
Create Date: 2026-08-27
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "0015_executor_channel_p1"
down_revision: str | None = "0014_proc_import_cloud"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "local_executors",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("owner_user_id", sa.Uuid(), nullable=False),
        sa.Column("display_name", sa.String(length=128), nullable=False),
        sa.Column("platform", sa.String(length=32), nullable=False),
        sa.Column("architecture", sa.String(length=32), nullable=False),
        sa.Column("client_version", sa.String(length=64), nullable=False),
        sa.Column("protocol_version", sa.Integer(), nullable=False),
        sa.Column("capabilities", sa.JSON(), nullable=False),
        sa.Column("credential_digest", sa.String(length=64), nullable=False),
        sa.Column("device_public_key", sa.Text()),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True)),
        sa.Column("config_revision", sa.String(length=128)),
        sa.Column("hub_status", sa.String(length=32), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("platform IN ('windows', 'macos')", name="ck_local_executor_platform"),
        sa.CheckConstraint("architecture IN ('x86_64', 'arm64')", name="ck_local_executor_architecture"),
        sa.CheckConstraint("protocol_version >= 1", name="ck_local_executor_protocol"),
        sa.CheckConstraint("status IN ('active', 'revoked')", name="ck_local_executor_status"),
        sa.CheckConstraint("hub_status IN ('unknown', 'ready', 'offline', 'limited')", name="ck_local_executor_hub_status"),
        sa.ForeignKeyConstraint(["owner_user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("credential_digest"),
    )
    op.create_index("ix_local_executor_tenant_status", "local_executors", ["tenant_id", "status"])
    op.create_index("ix_local_executor_last_seen", "local_executors", ["last_seen_at"])

    op.create_table(
        "executor_pairing_codes",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("created_by_user_id", sa.Uuid(), nullable=False),
        sa.Column("display_name_hint", sa.String(length=128)),
        sa.Column("code_digest", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True)),
        sa.Column("executor_id", sa.Uuid()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["executor_id"], ["local_executors.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("code_digest"),
    )
    op.create_index("ix_executor_pairing_expiry", "executor_pairing_codes", ["tenant_id", "expires_at"])

    op.create_table(
        "executor_tasks",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("executor_id", sa.Uuid(), nullable=False),
        sa.Column("task_type", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("payload_version", sa.Integer(), nullable=False),
        sa.Column("payload_envelope", sa.JSON(), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("lease_token_digest", sa.String(length=64)),
        sa.Column("lease_until", sa.DateTime(timezone=True)),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("created_by_user_id", sa.Uuid(), nullable=False),
        sa.Column("cancel_requested_at", sa.DateTime(timezone=True)),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("result_code", sa.String(length=128)),
        sa.Column("result_summary", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("task_type IN ('config.read.v1', 'config.write.v1')", name="ck_executor_task_type"),
        sa.CheckConstraint("status IN ('queued', 'leased', 'running', 'succeeded', 'failed', 'uncertain', 'cancel_requested', 'cancelled')", name="ck_executor_task_status"),
        sa.CheckConstraint("payload_version >= 1", name="ck_executor_task_payload_version"),
        sa.CheckConstraint("priority >= 0", name="ck_executor_task_priority"),
        sa.CheckConstraint("attempt >= 0", name="ck_executor_task_attempt"),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["executor_id"], ["local_executors.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "executor_id", "idempotency_key", name="uq_executor_task_idempotency"),
    )
    op.create_index("ix_executor_task_queue", "executor_tasks", ["executor_id", "status", "priority", "created_at"])
    op.create_index("ix_executor_task_lease", "executor_tasks", ["status", "lease_until"])

    op.create_table(
        "executor_task_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("executor_id", sa.Uuid(), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("phase", sa.String(length=64)),
        sa.Column("progress_current", sa.Integer()),
        sa.Column("progress_total", sa.Integer()),
        sa.Column("stable_code", sa.String(length=128)),
        sa.Column("trace_id", sa.String(length=64)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("progress_current IS NULL OR progress_current >= 0", name="ck_executor_task_event_current"),
        sa.CheckConstraint("progress_total IS NULL OR progress_total >= 0", name="ck_executor_task_event_total"),
        sa.ForeignKeyConstraint(["executor_id"], ["local_executors.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["task_id"], ["executor_tasks.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_executor_task_event_task", "executor_task_events", ["task_id", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_executor_task_event_task", table_name="executor_task_events")
    op.drop_table("executor_task_events")
    op.drop_index("ix_executor_task_lease", table_name="executor_tasks")
    op.drop_index("ix_executor_task_queue", table_name="executor_tasks")
    op.drop_table("executor_tasks")
    op.drop_index("ix_executor_pairing_expiry", table_name="executor_pairing_codes")
    op.drop_table("executor_pairing_codes")
    op.drop_index("ix_local_executor_last_seen", table_name="local_executors")
    op.drop_index("ix_local_executor_tenant_status", table_name="local_executors")
    op.drop_table("local_executors")
