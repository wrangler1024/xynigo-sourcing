from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Tenant(Base):
    __tablename__ = "tenants"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    feishu_tenant_key: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    name: Mapped[str | None] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (CheckConstraint("status IN ('active', 'disabled')", name="ck_tenant_status"),)


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)
    feishu_open_id: Mapped[str] = mapped_column(String(128), nullable=False)
    feishu_union_id: Mapped[str | None] = mapped_column(String(128))
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    avatar_url: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        UniqueConstraint("tenant_id", "feishu_open_id", name="uq_user_tenant_open_id"),
        CheckConstraint("status IN ('pending', 'active', 'disabled')", name="ck_user_status"),
        Index("ix_users_union_id", "feishu_union_id"),
    )


class Role(Base):
    __tablename__ = "roles"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)
    code: Mapped[str] = mapped_column(String(128), nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    is_system: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (UniqueConstraint("tenant_id", "code", name="uq_role_tenant_code"),)


class Permission(Base):
    __tablename__ = "permissions"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    code: Mapped[str] = mapped_column(String(160), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)


class UserRole(Base):
    __tablename__ = "user_roles"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    role_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("roles.id", ondelete="CASCADE"), primary_key=True
    )
    assigned_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class RolePermission(Base):
    __tablename__ = "role_permissions"

    role_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("roles.id", ondelete="CASCADE"), primary_key=True
    )
    permission_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("permissions.id", ondelete="CASCADE"), primary_key=True
    )


class SessionRecord(Base):
    __tablename__ = "sessions"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        Index("ix_sessions_user_active", "user_id", "expires_at", "revoked_at"),
    )


class LocalLoginRequest(Base):
    __tablename__ = "local_login_requests"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    poll_token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    denial_code: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'approved', 'denied', 'consumed')",
            name="ck_local_login_status",
        ),
        Index("ix_local_login_expiry", "expires_at"),
    )


class OAuthLoginAttempt(Base):
    __tablename__ = "oauth_login_attempts"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    state_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    code_verifier: Mapped[str | None] = mapped_column(String(128))
    local_login_request_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("local_login_requests.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (Index("ix_oauth_attempt_expiry", "expires_at"),)


class AuditEvent(Base):
    __tablename__ = "audit_events"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("tenants.id", ondelete="SET NULL")
    )
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    action: Mapped[str] = mapped_column(String(160), nullable=False)
    result: Mapped[str] = mapped_column(String(32), nullable=False)
    request_id: Mapped[str] = mapped_column(String(64), nullable=False)
    details: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        CheckConstraint("result IN ('success', 'denied', 'failure')", name="ck_audit_result"),
        Index("ix_audit_tenant_created", "tenant_id", "created_at"),
    )


class PurchaseOrder(Base):
    __tablename__ = "purchase_orders"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    order_key: Mapped[str] = mapped_column(String(800), nullable=False)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    draft_payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    draft_revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    execution_revision: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    submission_status: Mapped[str] = mapped_column(String(32), nullable=False, default="draft")
    sync_status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    created_by_user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    last_edited_by_user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    submitted_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        UniqueConstraint("tenant_id", "order_key", name="uq_purchase_order_tenant_key"),
        CheckConstraint("draft_revision >= 1", name="ck_purchase_order_revision"),
        CheckConstraint(
            "execution_revision >= 0",
            name="ck_purchase_order_execution_revision",
        ),
        CheckConstraint(
            "submission_status IN ('draft', 'submitted')",
            name="ck_purchase_order_submission_status",
        ),
        CheckConstraint(
            "sync_status IN ('pending', 'synced', 'failed', 'conflict')",
            name="ck_purchase_order_sync_status",
        ),
        Index("ix_purchase_order_tenant_updated", "tenant_id", "updated_at"),
    )


class PurchaseOrderLine(Base):
    __tablename__ = "purchase_order_lines"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    purchase_order_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("purchase_orders.id", ondelete="CASCADE"), nullable=False
    )
    line_key: Mapped[str] = mapped_column(String(900), nullable=False)
    line_no: Mapped[int] = mapped_column(Integer, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    workflow_status: Mapped[str] = mapped_column(String(32), nullable=False, default="draft")
    claimed_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        UniqueConstraint("purchase_order_id", "line_key", name="uq_purchase_line_order_key"),
        UniqueConstraint("purchase_order_id", "line_no", name="uq_purchase_line_order_no"),
        CheckConstraint("line_no >= 1", name="ck_purchase_line_number"),
        CheckConstraint(
            "workflow_status IN "
            "('draft', 'unclaimed', 'claimed', 'purchasing', 'ordered', "
            "'logistics_filled', 'completed', 'returned', 'exception')",
            name="ck_purchase_line_workflow_status",
        ),
        Index("ix_purchase_line_order_active", "purchase_order_id", "is_active"),
        Index("ix_purchase_line_claimant_status", "claimed_by_user_id", "workflow_status"),
    )


class PurchaseSplit(Base):
    __tablename__ = "purchase_splits"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    purchase_order_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("purchase_orders.id", ondelete="CASCADE"), nullable=False
    )
    split_no: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="waiting_binding"
    )
    purchaser_user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    site: Mapped[str] = mapped_column(String(20), nullable=False)
    hub_environment_ref: Mapped[str | None] = mapped_column(String(128))
    hub_environment_name: Mapped[str | None] = mapped_column(String(255))
    buyer_account_ref: Mapped[str | None] = mapped_column(String(128))
    buyer_account_label: Mapped[str | None] = mapped_column(String(255))
    platform_order_no: Mapped[str | None] = mapped_column(String(200))
    note: Mapped[str | None] = mapped_column(String(1000))
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        UniqueConstraint("tenant_id", "split_no", name="uq_purchase_split_tenant_no"),
        CheckConstraint(
            "status IN ('waiting_binding', 'waiting_order', 'purchasing', "
            "'ordered', 'exception')",
            name="ck_purchase_split_status",
        ),
        CheckConstraint("version >= 1", name="ck_purchase_split_version"),
        Index("ix_purchase_split_tenant_status", "tenant_id", "status", "updated_at"),
        Index(
            "ix_purchase_split_order_purchaser",
            "purchase_order_id",
            "purchaser_user_id",
        ),
    )


class PurchaseSplitLine(Base):
    __tablename__ = "purchase_split_lines"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    purchase_split_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("purchase_splits.id", ondelete="CASCADE"), nullable=False
    )
    purchase_order_line_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("purchase_order_lines.id", ondelete="RESTRICT"), nullable=False
    )
    allocated_qty: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        UniqueConstraint(
            "purchase_split_id",
            "purchase_order_line_id",
            name="uq_purchase_split_line",
        ),
        CheckConstraint("allocated_qty >= 1", name="ck_purchase_split_line_qty"),
        Index("ix_purchase_split_line_source", "purchase_order_line_id"),
    )


class PurchaseSyncOutbox(Base):
    __tablename__ = "purchase_sync_outbox"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    purchase_order_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("purchase_orders.id", ondelete="CASCADE"), nullable=False
    )
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    dedupe_key: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        CheckConstraint(
            "event_type IN ('draft.saved', 'order.submitted')",
            name="ck_purchase_sync_event_type",
        ),
        CheckConstraint(
            "status IN ('pending', 'processing', 'completed', 'failed')",
            name="ck_purchase_sync_status",
        ),
        CheckConstraint("attempt_count >= 0", name="ck_purchase_sync_attempt_count"),
        Index("ix_purchase_sync_pending", "status", "available_at"),
    )
