"""Postgres 表对应的 ORM 模型，相当于 Java JPA 的 @Entity。

每个 class 的 __tablename__ 就是表名；改字段后还要在 migrations/ 里加 Alembic 版本。
本文件只描述结构，不写业务查询。
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """所有表模型的基类，SQLAlchemy 用来发现元数据。"""

    pass


class Tenant(Base):
    """飞书企业（tenant_key）与 Xynigo 租户的映射。数据按租户隔离。"""

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
    """飞书用户。登录主键是 tenant_id + open_id，不是邮箱。新用户默认 pending。"""

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
    """租户内角色。super_admin / admin / member 为系统角色，不能删改代码。"""

    __tablename__ = "roles"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)
    code: Mapped[str] = mapped_column(String(128), nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    is_system: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (UniqueConstraint("tenant_id", "code", name="uq_role_tenant_code"),)


class Permission(Base):
    """系统权限码目录（如 procurement.access），全库共用，不按租户拆表。"""

    __tablename__ = "permissions"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    code: Mapped[str] = mapped_column(String(160), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)


class UserRole(Base):
    """用户 ↔ 角色 多对多。"""

    __tablename__ = "user_roles"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    role_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("roles.id", ondelete="CASCADE"), primary_key=True
    )
    assigned_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class RolePermission(Base):
    """角色 ↔ 权限 多对多。"""

    __tablename__ = "role_permissions"

    role_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("roles.id", ondelete="CASCADE"), primary_key=True
    )
    permission_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("permissions.id", ondelete="CASCADE"), primary_key=True
    )


class SessionRecord(Base):
    """可撤销登录会话。token_hash 是令牌 SHA-256，库里没有明文 session。"""

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
    """本机执行器登录桥：5 分钟内有效，poll 成功一次即 consumed。"""

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
    """飞书 OAuth 一次授权的 state/PKCE，用过即作废，防重放。"""

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
    """审计与业务操作日志。写入前会脱敏，不存密码、Cookie、Open ID、手机号等。"""

    __tablename__ = "audit_events"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("tenants.id", ondelete="SET NULL")
    )
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    actor_name: Mapped[str | None] = mapped_column(String(255))
    actor_roles: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    category: Mapped[str] = mapped_column(
        String(32), nullable=False, default="business_operation"
    )
    module: Mapped[str] = mapped_column(String(64), nullable=False, default="system")
    action: Mapped[str] = mapped_column(String(160), nullable=False)
    operation_type: Mapped[str] = mapped_column(String(160), nullable=False)
    result: Mapped[str] = mapped_column(String(32), nullable=False)
    outcome: Mapped[str] = mapped_column(String(32), nullable=False, default="success")
    business_object_type: Mapped[str | None] = mapped_column(String(64))
    business_object_id: Mapped[str | None] = mapped_column(String(160))
    business_object_no: Mapped[str | None] = mapped_column(String(255))
    failure_reason: Mapped[str | None] = mapped_column(String(160))
    change_summary: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    source: Mapped[str] = mapped_column(String(64), nullable=False, default="api")
    client_version: Mapped[str | None] = mapped_column(String(64))
    request_id: Mapped[str] = mapped_column(String(64), nullable=False)
    trace_id: Mapped[str] = mapped_column(String(64), nullable=False)
    details: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        CheckConstraint("result IN ('success', 'denied', 'failure')", name="ck_audit_result"),
        Index("ix_audit_tenant_created", "tenant_id", "created_at"),
        Index("ix_audit_tenant_category_created", "tenant_id", "category", "created_at"),
        Index("ix_audit_tenant_actor_created", "tenant_id", "actor_user_id", "created_at"),
        Index("ix_audit_tenant_module_created", "tenant_id", "module", "created_at"),
        Index("ix_audit_tenant_business_no", "tenant_id", "business_object_no"),
        Index("ix_audit_tenant_operation", "tenant_id", "operation_type"),
        Index("ix_audit_tenant_request", "tenant_id", "request_id"),
        Index("ix_audit_tenant_trace", "tenant_id", "trace_id"),
    )


class SystemLogEvent(Base):
    """独立的系统运行/错误日志；不保存请求正文、响应正文或堆栈正文。"""

    __tablename__ = "system_log_events"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("tenants.id", ondelete="SET NULL")
    )
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    actor_name: Mapped[str | None] = mapped_column(String(255))
    category: Mapped[str] = mapped_column(String(32), nullable=False)
    level: Mapped[str] = mapped_column(String(16), nullable=False)
    service: Mapped[str] = mapped_column(String(64), nullable=False)
    component: Mapped[str] = mapped_column(String(64), nullable=False)
    environment: Mapped[str] = mapped_column(String(32), nullable=False)
    event_type: Mapped[str] = mapped_column(String(160), nullable=False)
    message: Mapped[str] = mapped_column(String(500), nullable=False)
    http_method: Mapped[str | None] = mapped_column(String(16))
    route: Mapped[str | None] = mapped_column(String(255))
    status_code: Mapped[int | None] = mapped_column(Integer)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    exception_type: Mapped[str | None] = mapped_column(String(160))
    error_code: Mapped[str | None] = mapped_column(String(160))
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    source: Mapped[str] = mapped_column(String(64), nullable=False)
    client_version: Mapped[str | None] = mapped_column(String(64))
    request_id: Mapped[str] = mapped_column(String(64), nullable=False)
    trace_id: Mapped[str] = mapped_column(String(64), nullable=False)
    details: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        CheckConstraint(
            "category IN ('system_runtime', 'system_error')",
            name="ck_system_log_category",
        ),
        CheckConstraint(
            "level IN ('info', 'warning', 'error', 'critical')",
            name="ck_system_log_level",
        ),
        Index("ix_system_log_tenant_created", "tenant_id", "created_at"),
        Index(
            "ix_system_log_tenant_category_level_created",
            "tenant_id",
            "category",
            "level",
            "created_at",
        ),
        Index("ix_system_log_tenant_service_created", "tenant_id", "service", "created_at"),
        Index("ix_system_log_tenant_event_type", "tenant_id", "event_type"),
        Index("ix_system_log_tenant_status_created", "tenant_id", "status_code", "created_at"),
        Index("ix_system_log_tenant_request", "tenant_id", "request_id"),
        Index("ix_system_log_tenant_trace", "tenant_id", "trace_id"),
        Index("ix_system_log_fingerprint_created", "fingerprint", "created_at"),
        Index("ix_system_log_expires", "expires_at"),
    )


class PurchaseOrder(Base):
    """运营采购单。system_order_key 是对外键，order_key 保留兼容旧客户端。"""

    __tablename__ = "purchase_orders"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    order_key: Mapped[str] = mapped_column(String(800), nullable=False)
    system_order_key: Mapped[str] = mapped_column(String(32), nullable=False)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    store_name: Mapped[str] = mapped_column(String(300), nullable=False)
    store_base_name: Mapped[str] = mapped_column(String(300), nullable=False)
    operator_name: Mapped[str | None] = mapped_column(String(100))
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
    feishu_record_id: Mapped[str | None] = mapped_column(String(128), unique=True)
    feishu_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sync_error_code: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        UniqueConstraint("tenant_id", "order_key", name="uq_purchase_order_tenant_key"),
        UniqueConstraint(
            "tenant_id",
            "system_order_key",
            name="uq_purchase_order_tenant_system_key",
        ),
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
        Index(
            "ix_purchase_order_tenant_operator_updated",
            "tenant_id",
            "operator_name",
            "updated_at",
        ),
        Index(
            "ix_purchase_order_tenant_store_base",
            "tenant_id",
            "store_base_name",
        ),
    )


class PurchaseOrderLine(Base):
    """采购明细。workflow_status 表示认领/下单/物流等执行状态。"""

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
    feishu_record_id: Mapped[str | None] = mapped_column(String(128), unique=True)
    feishu_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
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
    """一张单拆给某个采购员执行的批次，可绑 Hub 环境与买家号。"""

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
    """拆分批次包含哪些原单明细，以及分配数量。"""

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


class BuyerAccount(Base):
    """PostgreSQL-authoritative buyer account and encrypted credential envelope."""

    __tablename__ = "buyer_accounts"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    account_ref: Mapped[str] = mapped_column(String(128), nullable=False)
    display_label: Mapped[str] = mapped_column(String(255), nullable=False)
    site: Mapped[str] = mapped_column(String(20), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="available")
    source_availability_status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="available"
    )
    credential_status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="unknown"
    )
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    source_status: Mapped[str | None] = mapped_column(String(64))
    source_vendor_label: Mapped[str | None] = mapped_column(String(100))
    source_batch_ref: Mapped[str | None] = mapped_column(String(128))
    source_purchase_date: Mapped[date | None] = mapped_column(Date)
    source_order_ref: Mapped[str | None] = mapped_column(String(128))
    credentials_ciphertext: Mapped[str | None] = mapped_column(Text)
    source_business_profile: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict
    )
    hub_environment_ref: Mapped[str | None] = mapped_column(String(128))
    hub_environment_name: Mapped[str | None] = mapped_column(String(255))
    operator_label: Mapped[str | None] = mapped_column(String(100))
    current_checkout_attempt_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("checkout_attempts.id", ondelete="SET NULL")
    )
    last_snapshot_key: Mapped[str | None] = mapped_column(String(128))
    source_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_synced_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    feishu_sync_status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="pending"
    )
    feishu_record_id: Mapped[str | None] = mapped_column(String(128))
    feishu_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "account_ref", name="uq_buyer_account_tenant_ref"
        ),
        UniqueConstraint(
            "current_checkout_attempt_id", name="uq_buyer_account_current_attempt"
        ),
        UniqueConstraint(
            "tenant_id", "source_order_ref", name="uq_buyer_account_tenant_source_order"
        ),
        CheckConstraint("site IN ('US', 'MX')", name="ck_buyer_account_site"),
        CheckConstraint(
            "status IN ('available', 'reserved', 'in_use', 'cleanup_pending', "
            "'post_payment_hold', 'manual_review', 'disabled')",
            name="ck_buyer_account_status",
        ),
        CheckConstraint(
            "source_availability_status IN ('available', 'manual_review', 'disabled')",
            name="ck_buyer_account_source_availability_status",
        ),
        CheckConstraint(
            "credential_status IN ('ready', 'unverified', 'invalid', 'unknown')",
            name="ck_buyer_account_credential_status",
        ),
        CheckConstraint(
            "feishu_sync_status IN ('pending', 'processing', 'completed', 'failed')",
            name="ck_buyer_account_feishu_status",
        ),
        CheckConstraint("version >= 1", name="ck_buyer_account_version"),
        Index(
            "ix_buyer_account_tenant_site_status",
            "tenant_id",
            "site",
            "status",
            "updated_at",
        ),
        Index(
            "ix_buyer_account_tenant_credential",
            "tenant_id",
            "credential_status",
            "status",
        ),
    )


class CheckoutAttempt(Base):
    """付款前可修改的下单尝试；非终态尝试会占用采购明细数量。"""

    __tablename__ = "checkout_attempts"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    purchase_order_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("purchase_orders.id", ondelete="CASCADE"), nullable=False
    )
    purchaser_user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    attempt_no: Mapped[str] = mapped_column(String(64), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="planning")
    site: Mapped[str] = mapped_column(String(20), nullable=False)
    hub_environment_ref: Mapped[str | None] = mapped_column(String(128))
    hub_environment_name: Mapped[str | None] = mapped_column(String(255))
    buyer_account_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("buyer_accounts.id", ondelete="RESTRICT")
    )
    buyer_account_ref: Mapped[str | None] = mapped_column(String(128))
    buyer_account_label: Mapped[str | None] = mapped_column(String(255))
    resource_status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="unbound"
    )
    pending_terminal_status: Mapped[str | None] = mapped_column(String(32))
    note: Mapped[str | None] = mapped_column(String(1000))
    terminal_reason: Mapped[str | None] = mapped_column(String(500))
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    payment_recorded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    terminal_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        UniqueConstraint("tenant_id", "attempt_no", name="uq_checkout_attempt_tenant_no"),
        UniqueConstraint(
            "tenant_id",
            "idempotency_key",
            name="uq_checkout_attempt_tenant_idempotency",
        ),
        CheckConstraint(
            "status IN ('planning', 'ready', 'checkout', 'cleanup_pending', "
            "'manual_review', 'paid', 'failed', 'abandoned')",
            name="ck_checkout_attempt_status",
        ),
        CheckConstraint(
            "resource_status IN ('unbound', 'reserved', 'active', 'cleanup_pending', "
            "'released', 'retained', 'manual_review')",
            name="ck_checkout_attempt_resource_status",
        ),
        CheckConstraint(
            "pending_terminal_status IS NULL OR "
            "pending_terminal_status IN ('failed', 'abandoned')",
            name="ck_checkout_attempt_pending_terminal",
        ),
        CheckConstraint("version >= 1", name="ck_checkout_attempt_version"),
        CheckConstraint(
            "(hub_environment_ref IS NULL AND buyer_account_id IS NULL AND "
            "buyer_account_ref IS NULL) OR "
            "(hub_environment_ref IS NOT NULL AND buyer_account_id IS NOT NULL AND "
            "buyer_account_ref IS NOT NULL)",
            name="ck_checkout_attempt_resource_pair",
        ),
        Index(
            "ix_checkout_attempt_order_status",
            "purchase_order_id",
            "status",
            "updated_at",
        ),
        Index(
            "ix_checkout_attempt_purchaser_status",
            "purchaser_user_id",
            "status",
            "updated_at",
        ),
        Index(
            "ix_checkout_attempt_buyer_account",
            "buyer_account_id",
            "status",
        ),
        Index(
            "uq_checkout_attempt_active_hub",
            "tenant_id",
            "hub_environment_ref",
            unique=True,
            postgresql_where=(
                status.in_(
                    (
                        "planning",
                        "ready",
                        "checkout",
                        "cleanup_pending",
                        "manual_review",
                    )
                )
                & hub_environment_ref.is_not(None)
            ),
            sqlite_where=(
                status.in_(
                    (
                        "planning",
                        "ready",
                        "checkout",
                        "cleanup_pending",
                        "manual_review",
                    )
                )
                & hub_environment_ref.is_not(None)
            ),
        ),
        Index(
            "uq_checkout_attempt_active_buyer",
            "tenant_id",
            "buyer_account_ref",
            unique=True,
            postgresql_where=(
                status.in_(
                    (
                        "planning",
                        "ready",
                        "checkout",
                        "cleanup_pending",
                        "manual_review",
                    )
                )
                & buyer_account_ref.is_not(None)
            ),
            sqlite_where=(
                status.in_(
                    (
                        "planning",
                        "ready",
                        "checkout",
                        "cleanup_pending",
                        "manual_review",
                    )
                )
                & buyer_account_ref.is_not(None)
            ),
        ),
    )


class CheckoutAttemptLine(Base):
    """一次下单尝试占用的采购明细数量。"""

    __tablename__ = "checkout_attempt_lines"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    checkout_attempt_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("checkout_attempts.id", ondelete="CASCADE"), nullable=False
    )
    purchase_order_line_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("purchase_order_lines.id", ondelete="RESTRICT"), nullable=False
    )
    reserved_qty: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        UniqueConstraint(
            "checkout_attempt_id",
            "purchase_order_line_id",
            name="uq_checkout_attempt_line",
        ),
        CheckConstraint("reserved_qty >= 1", name="ck_checkout_attempt_line_qty"),
        Index("ix_checkout_attempt_line_source", "purchase_order_line_id"),
    )


class PurchaseBatch(Base):
    """付款成功后形成的正式采购批次；一条下单尝试至多生成一批。"""

    __tablename__ = "purchase_batches"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    purchase_order_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("purchase_orders.id", ondelete="CASCADE"), nullable=False
    )
    checkout_attempt_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("checkout_attempts.id", ondelete="RESTRICT"), nullable=False
    )
    purchaser_user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    batch_no: Mapped[str] = mapped_column(String(64), nullable=False)
    platform: Mapped[str] = mapped_column(String(32), nullable=False)
    platform_order_no: Mapped[str] = mapped_column(String(200), nullable=False)
    site: Mapped[str] = mapped_column(String(20), nullable=False)
    actual_amount: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(12), nullable=False)
    discount_amount: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))
    coupon_summary: Mapped[str | None] = mapped_column(String(500))
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="paid")
    hub_environment_ref: Mapped[str] = mapped_column(String(128), nullable=False)
    hub_environment_name: Mapped[str] = mapped_column(String(255), nullable=False)
    buyer_account_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("buyer_accounts.id", ondelete="RESTRICT")
    )
    buyer_account_ref: Mapped[str] = mapped_column(String(128), nullable=False)
    buyer_account_label: Mapped[str] = mapped_column(String(255), nullable=False)
    paid_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        UniqueConstraint("checkout_attempt_id", name="uq_purchase_batch_attempt"),
        UniqueConstraint("tenant_id", "batch_no", name="uq_purchase_batch_tenant_no"),
        UniqueConstraint(
            "tenant_id",
            "platform",
            "platform_order_no",
            name="uq_purchase_batch_platform_order",
        ),
        UniqueConstraint(
            "tenant_id",
            "hub_environment_ref",
            name="uq_purchase_batch_hub_environment",
        ),
        UniqueConstraint(
            "tenant_id",
            "buyer_account_ref",
            name="uq_purchase_batch_buyer_account",
        ),
        CheckConstraint(
            "status IN ('paid', 'tracking', 'completed', 'exception')",
            name="ck_purchase_batch_status",
        ),
        CheckConstraint("actual_amount >= 0", name="ck_purchase_batch_actual_amount"),
        CheckConstraint(
            "discount_amount IS NULL OR discount_amount >= 0",
            name="ck_purchase_batch_discount_amount",
        ),
        Index(
            "ix_purchase_batch_order_status",
            "purchase_order_id",
            "status",
            "updated_at",
        ),
        Index(
            "ix_purchase_batch_purchaser_status",
            "purchaser_user_id",
            "status",
            "updated_at",
        ),
        Index(
            "ix_purchase_batch_buyer_account",
            "buyer_account_id",
            "paid_at",
        ),
    )


class PurchaseBatchLine(Base):
    """正式采购批次包含的采购明细和已购数量。"""

    __tablename__ = "purchase_batch_lines"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    purchase_batch_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("purchase_batches.id", ondelete="CASCADE"), nullable=False
    )
    purchase_order_line_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("purchase_order_lines.id", ondelete="RESTRICT"), nullable=False
    )
    purchased_qty: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        UniqueConstraint(
            "purchase_batch_id",
            "purchase_order_line_id",
            name="uq_purchase_batch_line",
        ),
        CheckConstraint("purchased_qty >= 1", name="ck_purchase_batch_line_qty"),
        Index("ix_purchase_batch_line_source", "purchase_order_line_id"),
    )


class SupplierShipment(Base):
    """一个正式采购批次可拆成多个承运包裹和物流单号。"""

    __tablename__ = "supplier_shipments"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    purchase_batch_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("purchase_batches.id", ondelete="CASCADE"), nullable=False
    )
    shipment_key: Mapped[str] = mapped_column(String(128), nullable=False)
    package_no: Mapped[str | None] = mapped_column(String(200))
    carrier_code: Mapped[str | None] = mapped_column(String(64))
    carrier_name: Mapped[str] = mapped_column(String(128), nullable=False)
    tracking_no: Mapped[str] = mapped_column(String(200), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    shipped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        UniqueConstraint(
            "purchase_batch_id",
            "shipment_key",
            name="uq_supplier_shipment_batch_key",
        ),
        UniqueConstraint(
            "purchase_batch_id",
            "tracking_no",
            name="uq_supplier_shipment_tracking",
        ),
        CheckConstraint(
            "status IN ('pending_pickup', 'in_transit', 'delivered', 'exception')",
            name="ck_supplier_shipment_status",
        ),
        CheckConstraint("version >= 1", name="ck_supplier_shipment_version"),
        Index(
            "ix_supplier_shipment_batch_status",
            "purchase_batch_id",
            "status",
            "updated_at",
        ),
    )


class EnvironmentCreationRun(Base):
    """One local buyer-account environment-creation execution."""

    __tablename__ = "environment_creation_runs"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    actor_user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    source_run_key: Mapped[str] = mapped_column(String(128), nullable=False)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    result_payload_hash: Mapped[str | None] = mapped_column(String(64))
    executor_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("local_executors.id", ondelete="SET NULL")
    )
    executor_task_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("executor_tasks.id", ondelete="SET NULL")
    )
    parent_run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("environment_creation_runs.id", ondelete="SET NULL")
    )
    run_mode: Mapped[str] = mapped_column(String(32), nullable=False, default="bound")
    site: Mapped[str] = mapped_column(String(20), nullable=False)
    purchase_date: Mapped[str] = mapped_column(String(8), nullable=False)
    environment_group: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    phase: Mapped[str] = mapped_column(String(64), nullable=False, default="created")
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    progress_completed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    progress_total: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    stop_requested: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    total_count: Mapped[int] = mapped_column(Integer, nullable=False)
    success_count: Mapped[int] = mapped_column(Integer, nullable=False)
    failed_count: Mapped[int] = mapped_column(Integer, nullable=False)
    ip_ok_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    ip_total_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    request_summary: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    source: Mapped[str] = mapped_column(String(64), nullable=False)
    client_version: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "source_run_key", name="uq_environment_run_tenant_source"
        ),
        CheckConstraint("site IN ('US', 'MX')", name="ck_environment_run_site"),
        CheckConstraint(
            "status IN ('created', 'queued', 'leased', 'running', "
            "'completed', 'partial_failure', 'failed', 'cancelled', 'uncertain')",
            name="ck_environment_run_status",
        ),
        CheckConstraint(
            "run_mode IN ('bound', 'backup', 'test', 'retry_row', 'retry_failed')",
            name="ck_environment_run_mode",
        ),
        CheckConstraint(
            "total_count >= 0 AND success_count >= 0 AND failed_count >= 0 "
            "AND attempt >= 0 AND progress_completed >= 0 AND progress_total >= 0 "
            "AND progress_completed <= progress_total",
            name="ck_environment_run_counts",
        ),
        Index("ix_environment_run_tenant_completed", "tenant_id", "completed_at"),
        Index("ix_environment_run_tenant_status", "tenant_id", "status", "updated_at"),
        Index("ix_environment_run_executor_task", "executor_task_id", unique=True),
        Index("ix_environment_run_parent", "parent_run_id", "created_at"),
    )


class EnvironmentCreationResult(Base):
    """Credential-free result for one buyer account / Hub environment pair."""

    __tablename__ = "environment_creation_results"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("environment_creation_runs.id", ondelete="CASCADE"), nullable=False
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    account_ref: Mapped[str] = mapped_column(String(128), nullable=False)
    account_label: Mapped[str] = mapped_column(String(255), nullable=False)
    purchaser_label: Mapped[str] = mapped_column(String(100), nullable=False)
    environment_name: Mapped[str] = mapped_column(String(255), nullable=False)
    environment_ref: Mapped[str | None] = mapped_column(String(128))
    environment_serial: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    current_step: Mapped[str | None] = mapped_column(String(64))
    completed_steps: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    error_step: Mapped[str | None] = mapped_column(String(64))
    error_summary: Mapped[str | None] = mapped_column(String(300))
    binding_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    recovered_existing: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_in_run: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    cleanup_status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="not_required"
    )
    cleanup_error_code: Mapped[str | None] = mapped_column(String(128))
    cleanup_error_summary: Mapped[str | None] = mapped_column(String(300))
    ip_address: Mapped[str | None] = mapped_column(String(64))
    ip_country: Mapped[str | None] = mapped_column(String(100))
    ip_city: Mapped[str | None] = mapped_column(String(100))
    ip_isp: Mapped[str | None] = mapped_column(String(200))
    ip_verified: Mapped[bool | None] = mapped_column(Boolean)
    ip_error_code: Mapped[str | None] = mapped_column(String(128))
    ip_error_summary: Mapped[str | None] = mapped_column(String(300))
    feishu_sync_status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="pending"
    )
    feishu_record_id: Mapped[str | None] = mapped_column(String(128))
    feishu_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        UniqueConstraint("run_id", "account_ref", name="uq_environment_result_run_account"),
        CheckConstraint(
            "status IN ('queued', 'running', 'success', 'failed', 'stopped')",
            name="ck_environment_result_status",
        ),
        CheckConstraint(
            "cleanup_status IN ('not_required', 'pending', 'deleting', "
            "'deleted', 'failed')",
            name="ck_environment_result_cleanup_status",
        ),
        CheckConstraint(
            "feishu_sync_status IN ('pending', 'processing', 'completed', 'failed')",
            name="ck_environment_result_feishu_status",
        ),
        Index("ix_environment_result_tenant_created", "tenant_id", "created_at"),
        Index("ix_environment_result_environment_ref", "tenant_id", "environment_ref"),
    )


class EnvironmentAccountRunGuard(Base):
    """Persistent tenant/account barrier across create and cleanup Runs."""

    __tablename__ = "environment_account_run_guards"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    account_ref: Mapped[str] = mapped_column(String(128), nullable=False)
    run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("environment_creation_runs.id", ondelete="CASCADE"), nullable=False
    )
    state: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "account_ref",
            name="uq_environment_account_run_guard",
        ),
        CheckConstraint(
            "state IN ('active', 'cleanup_pending', 'cleanup_failed')",
            name="ck_environment_account_run_guard_state",
        ),
        Index("ix_environment_account_run_guard_run", "run_id", "state"),
    )


class LogisticsQueryRun(Base):
    """One local SHEIN order / tracking query execution."""

    __tablename__ = "logistics_query_runs"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    actor_user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    source_run_key: Mapped[str] = mapped_column(String(128), nullable=False)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    result_payload_hash: Mapped[str | None] = mapped_column(String(64))
    executor_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("local_executors.id", ondelete="SET NULL")
    )
    executor_task_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("executor_tasks.id", ondelete="SET NULL")
    )
    query_mode: Mapped[str] = mapped_column(String(32), nullable=False)
    site: Mapped[str] = mapped_column(String(20), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    phase: Mapped[str] = mapped_column(String(64), nullable=False, default="created")
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    progress_completed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    progress_total: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    stop_requested: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    total_count: Mapped[int] = mapped_column(Integer, nullable=False)
    success_count: Mapped[int] = mapped_column(Integer, nullable=False)
    failed_count: Mapped[int] = mapped_column(Integer, nullable=False)
    request_summary: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    source: Mapped[str] = mapped_column(String(64), nullable=False)
    client_version: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "source_run_key", name="uq_logistics_run_tenant_source"
        ),
        CheckConstraint("site IN ('US', 'MX')", name="ck_logistics_run_site"),
        CheckConstraint(
            "query_mode IN ('initial', 'single_retry', 'failed_retry')",
            name="ck_logistics_run_mode",
        ),
        CheckConstraint(
            "status IN ('created', 'queued', 'leased', 'running', "
            "'completed', 'partial_failure', 'failed', 'cancelled', 'uncertain')",
            name="ck_logistics_run_status",
        ),
        CheckConstraint(
            "total_count >= 0 AND success_count >= 0 AND failed_count >= 0 "
            "AND attempt >= 0 AND progress_completed >= 0 AND progress_total >= 0 "
            "AND progress_completed <= progress_total",
            name="ck_logistics_run_counts",
        ),
        Index("ix_logistics_run_tenant_completed", "tenant_id", "completed_at"),
        Index("ix_logistics_run_tenant_status", "tenant_id", "status", "updated_at"),
        Index("ix_logistics_run_executor_task", "executor_task_id", unique=True),
    )


class LogisticsQueryResult(Base):
    """One environment's durable order and tracking lookup result."""

    __tablename__ = "logistics_query_results"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("logistics_query_runs.id", ondelete="CASCADE"), nullable=False
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    environment_serial: Mapped[str] = mapped_column(String(64), nullable=False)
    environment_name: Mapped[str | None] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    current_step: Mapped[str | None] = mapped_column(String(64))
    completed_steps: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    platform_order_no: Mapped[str | None] = mapped_column(String(160))
    order_time_text: Mapped[str | None] = mapped_column(String(64))
    amount_text: Mapped[str | None] = mapped_column(String(64))
    platform_status: Mapped[str | None] = mapped_column(String(100))
    status_label: Mapped[str | None] = mapped_column(String(100))
    fulfillment_stage: Mapped[str | None] = mapped_column(String(100))
    tracking_numbers: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    package_numbers: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    carrier: Mapped[str | None] = mapped_column(String(100))
    cancelled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    risk_order: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    risk_summary: Mapped[str | None] = mapped_column(String(300))
    ip_address: Mapped[str | None] = mapped_column(String(64))
    time_zone: Mapped[str | None] = mapped_column(String(100))
    utc_offset_minutes: Mapped[int | None] = mapped_column(Integer)
    queried_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_summary: Mapped[str | None] = mapped_column(String(300))
    screenshot_status: Mapped[str | None] = mapped_column(String(32))
    screenshot_content: Mapped[bytes | None] = mapped_column(LargeBinary)
    screenshot_content_type: Mapped[str | None] = mapped_column(String(64))
    screenshot_sha256: Mapped[str | None] = mapped_column(String(64))
    screenshot_size: Mapped[int | None] = mapped_column(Integer)
    screenshot_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    feishu_sync_status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="pending"
    )
    feishu_record_id: Mapped[str | None] = mapped_column(String(128))
    feishu_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        UniqueConstraint("run_id", "environment_serial", name="uq_logistics_result_run_env"),
        CheckConstraint(
            "status IN ('ok', 'fail', 'login', 'inuse', 'stopped', 'pending', 'running')",
            name="ck_logistics_result_status",
        ),
        CheckConstraint(
            "feishu_sync_status IN ('pending', 'processing', 'completed', 'failed')",
            name="ck_logistics_result_feishu_status",
        ),
        Index("ix_logistics_result_tenant_created", "tenant_id", "created_at"),
        Index("ix_logistics_result_order", "tenant_id", "platform_order_no"),
    )


class OperationalSyncOutbox(Base):
    """Retryable Feishu mirror events for buyer and daily operational facts."""

    __tablename__ = "operational_sync_outbox"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    aggregate_type: Mapped[str] = mapped_column(String(64), nullable=False)
    aggregate_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    dedupe_key: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    external_record_id: Mapped[str | None] = mapped_column(String(128))
    last_error_code: Mapped[str | None] = mapped_column(String(128))
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "aggregate_type IN ('buyer_account', 'environment_creation_result', "
            "'logistics_query_result')",
            name="ck_operational_sync_aggregate_type",
        ),
        CheckConstraint(
            "status IN ('pending', 'processing', 'completed', 'failed')",
            name="ck_operational_sync_status",
        ),
        CheckConstraint("attempt_count >= 0", name="ck_operational_sync_attempt_count"),
        Index("ix_operational_sync_pending", "status", "available_at"),
    )


class ProcurementImportPlan(Base):
    """Encrypted, short-lived XYP2 collaboration import plan."""

    __tablename__ = "procurement_import_plans"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    created_by_user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    import_batch: Mapped[str] = mapped_column(String(128), nullable=False)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    encrypted_payload: Mapped[bytes | None] = mapped_column(LargeBinary)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="parsed")
    source_row_count: Mapped[int] = mapped_column(Integer, nullable=False)
    order_count: Mapped[int] = mapped_column(Integer, nullable=False)
    detail_count: Mapped[int] = mapped_column(Integer, nullable=False)
    image_count: Mapped[int] = mapped_column(Integer, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('parsed', 'validated', 'expired')",
            name="ck_procurement_import_plan_status",
        ),
        CheckConstraint(
            "source_row_count >= 0 AND order_count >= 0 AND "
            "detail_count >= 0 AND image_count >= 0",
            name="ck_procurement_import_plan_counts",
        ),
        Index(
            "ix_procurement_import_plan_tenant_expiry",
            "tenant_id",
            "expires_at",
        ),
    )


class EnvironmentAccountPlan(Base):
    """Encrypted, short-lived buyer-account plan parsed by the cloud."""

    __tablename__ = "environment_account_plans"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    created_by_user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    site: Mapped[str] = mapped_column(String(8), nullable=False)
    environment_group: Mapped[str] = mapped_column(String(255), nullable=False)
    source_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    preview_summary: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict
    )
    encrypted_payload: Mapped[bytes | None] = mapped_column(LargeBinary)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="parsed")
    account_count: Mapped[int] = mapped_column(Integer, nullable=False)
    cookie_count: Mapped[int] = mapped_column(Integer, nullable=False)
    mixed_site_cookie_count: Mapped[int] = mapped_column(Integer, nullable=False)
    password_kind_count: Mapped[int] = mapped_column(Integer, nullable=False)
    order_count: Mapped[int] = mapped_column(Integer, nullable=False)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "created_by_user_id",
            "idempotency_key",
            name="uq_environment_account_plan_idempotency",
        ),
        CheckConstraint("site IN ('US', 'MX')", name="ck_environment_account_plan_site"),
        CheckConstraint(
            "status IN ('parsed', 'submitted', 'expired')",
            name="ck_environment_account_plan_status",
        ),
        CheckConstraint(
            "account_count >= 1 AND cookie_count >= 0 AND "
            "mixed_site_cookie_count >= 0 AND password_kind_count >= 0 AND "
            "order_count >= 0",
            name="ck_environment_account_plan_counts",
        ),
        Index(
            "ix_environment_account_plan_tenant_expiry",
            "tenant_id",
            "expires_at",
        ),
        Index(
            "ix_environment_account_plan_user_latest",
            "tenant_id",
            "created_by_user_id",
            "created_at",
        ),
        Index(
            "uq_environment_account_plan_active_source",
            "tenant_id",
            "created_by_user_id",
            "source_hash",
            unique=True,
            postgresql_where=text("status = 'parsed'"),
            sqlite_where=text("status = 'parsed'"),
        ),
    )


class EnvironmentWorkspacePreference(Base):
    """Cloud-owned site/group preference; never occupies an executor task."""

    __tablename__ = "environment_workspace_preferences"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), primary_key=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    purchase_site: Mapped[str] = mapped_column(String(8), nullable=False, default="MX")
    purchase_tags: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "purchase_site IN ('US', 'MX')",
            name="ck_environment_workspace_preference_site",
        ),
    )


class WorkspaceViewPreference(Base):
    """Reusable per-user presentation preferences for one workspace view."""

    __tablename__ = "workspace_view_preferences"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), primary_key=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    view_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    settings: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "schema_version >= 1", name="ck_workspace_view_preference_schema"
        ),
    )


class TenantFeishuIntegration(Base):
    """One encrypted Feishu enterprise-app credential per tenant."""

    __tablename__ = "tenant_feishu_integrations"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), primary_key=True
    )
    app_id: Mapped[str] = mapped_column(String(128), nullable=False)
    credential_ciphertext: Mapped[str] = mapped_column(Text, nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    configured_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    verified_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        CheckConstraint("revision >= 1", name="ck_tenant_feishu_integration_revision"),
    )


class EnvironmentAccountPlanRequest(Base):
    """Exact idempotency replay for cloud environment-plan parse requests."""

    __tablename__ = "environment_account_plan_requests"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    created_by_user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    cloud_plan_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("environment_account_plans.id", ondelete="CASCADE"),
        nullable=False,
    )
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    source_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    reused: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "created_by_user_id",
            "idempotency_key",
            name="uq_environment_account_plan_request_idempotency",
        ),
        Index(
            "ix_environment_account_plan_request_plan",
            "cloud_plan_id",
            "created_at",
        ),
    )


class ProcurementImportJob(Base):
    """Durable progress for an idempotent ordinary-Sheet import run."""

    __tablename__ = "procurement_import_jobs"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    plan_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("procurement_import_plans.id", ondelete="CASCADE"), nullable=False
    )
    created_by_user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    target_key_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False, default="queued")
    progress: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    last_error_code: Mapped[str | None] = mapped_column(String(128))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "state IN ('queued', 'validating', 'normalizing_headers', "
            "'formatting_headers', 'writing_rows', 'verifying_rows', "
            "'formatting_rows', 'writing_links', 'writing_images', "
            "'completed', 'partial', 'failed')",
            name="ck_procurement_import_job_state",
        ),
        Index(
            "ix_procurement_import_job_pending",
            "state",
            "created_at",
        ),
        Index(
            "ix_procurement_import_job_target",
            "tenant_id",
            "target_key_hash",
        ),
        Index(
            "uq_procurement_import_job_active_target",
            "tenant_id",
            "target_key_hash",
            unique=True,
            postgresql_where=text(
                "state IN ('queued', 'validating', 'normalizing_headers', "
                "'formatting_headers', 'writing_rows', 'verifying_rows', "
                "'formatting_rows', 'writing_links', 'writing_images')"
            ),
            sqlite_where=text(
                "state IN ('queued', 'validating', 'normalizing_headers', "
                "'formatting_headers', 'writing_rows', 'verifying_rows', "
                "'formatting_rows', 'writing_links', 'writing_images')"
            ),
        ),
    )


class LocalExecutor(Base):
    """A tenant-bound local executor authenticated by a device credential."""

    __tablename__ = "local_executors"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    owner_user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    display_name: Mapped[str] = mapped_column(String(128), nullable=False)
    platform: Mapped[str] = mapped_column(String(32), nullable=False)
    architecture: Mapped[str] = mapped_column(String(32), nullable=False)
    client_version: Mapped[str] = mapped_column(String(64), nullable=False)
    protocol_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    capabilities: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    credential_digest: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    device_public_key: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    config_revision: Mapped[str | None] = mapped_column(String(128))
    hub_status: Mapped[str] = mapped_column(String(32), nullable=False, default="unknown")
    workspace_snapshot: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict
    )
    workspace_snapshot_revision: Mapped[str | None] = mapped_column(String(64))
    workspace_snapshot_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        CheckConstraint("platform IN ('windows', 'macos')", name="ck_local_executor_platform"),
        CheckConstraint(
            "architecture IN ('x86_64', 'arm64')",
            name="ck_local_executor_architecture",
        ),
        CheckConstraint("protocol_version >= 1", name="ck_local_executor_protocol"),
        CheckConstraint(
            "status IN ('active', 'revoked')", name="ck_local_executor_status"
        ),
        CheckConstraint(
            "hub_status IN ('unknown', 'ready', 'offline', 'limited')",
            name="ck_local_executor_hub_status",
        ),
        Index("ix_local_executor_tenant_status", "tenant_id", "status"),
        Index("ix_local_executor_last_seen", "last_seen_at"),
    )


class ExecutorPairingCode(Base):
    """Short-lived single-use code that binds one executor to a tenant."""

    __tablename__ = "executor_pairing_codes"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    created_by_user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    display_name_hint: Mapped[str | None] = mapped_column(String(128))
    code_digest: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    executor_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("local_executors.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (
        Index("ix_executor_pairing_expiry", "tenant_id", "expires_at"),
    )


class ExecutorTask(Base):
    """Durable cloud task leased to one local executor."""

    __tablename__ = "executor_tasks"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    executor_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("local_executors.id", ondelete="CASCADE"), nullable=False
    )
    task_type: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="queued")
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    payload_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    payload_envelope: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=100)
    lease_token_digest: Mapped[str | None] = mapped_column(String(64))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_by_user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    cancel_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    result_code: Mapped[str | None] = mapped_column(String(128))
    result_summary: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "executor_id",
            "idempotency_key",
            name="uq_executor_task_idempotency",
        ),
        CheckConstraint(
            "task_type IN ('config.read.v1', 'config.write.v1', "
            "'workspace.rpc.v1', 'workspace.snapshot.v1', "
            "'environment.parse.v1', 'logistics.query.v1', "
            "'environment.create-bound.v1', 'environment.create-backup.v1', "
            "'environment.retry-row.v1', 'environment.retry-failed.v1')",
            name="ck_executor_task_type",
        ),
        CheckConstraint(
            "status IN ('queued', 'leased', 'running', 'succeeded', 'failed', "
            "'uncertain', 'cancel_requested', 'cancelled')",
            name="ck_executor_task_status",
        ),
        CheckConstraint("payload_version >= 1", name="ck_executor_task_payload_version"),
        CheckConstraint("priority >= 0", name="ck_executor_task_priority"),
        CheckConstraint("attempt >= 0", name="ck_executor_task_attempt"),
        Index("ix_executor_task_queue", "executor_id", "status", "priority", "created_at"),
        Index("ix_executor_task_lease", "status", "lease_until"),
    )


class ExecutorTaskEvent(Base):
    """Append-only, redacted executor task state and progress event."""

    __tablename__ = "executor_task_events"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    task_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("executor_tasks.id", ondelete="CASCADE"), nullable=False
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    executor_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("local_executors.id", ondelete="CASCADE"), nullable=False
    )
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    phase: Mapped[str | None] = mapped_column(String(64))
    progress_current: Mapped[int | None] = mapped_column(Integer)
    progress_total: Mapped[int | None] = mapped_column(Integer)
    stable_code: Mapped[str | None] = mapped_column(String(128))
    trace_id: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "progress_current IS NULL OR progress_current >= 0",
            name="ck_executor_task_event_current",
        ),
        CheckConstraint(
            "progress_total IS NULL OR progress_total >= 0",
            name="ck_executor_task_event_total",
        ),
        Index("ix_executor_task_event_task", "task_id", "created_at"),
    )


class PurchaseSyncOutbox(Base):
    """飞书镜像同步发件箱。与采购业务变更同一事务写入。"""

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
    last_error_code: Mapped[str | None] = mapped_column(String(128))
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "event_type IN ('draft.saved', 'order.submitted', 'checkout.attempted', "
            "'order.assignment_changed', 'order.execution_changed', 'checkout.updated', "
            "'checkout.started', 'checkout.abandoned', "
            "'checkout.failed', 'purchase.paid', 'shipment.updated')",
            name="ck_purchase_sync_event_type",
        ),
        CheckConstraint(
            "status IN ('pending', 'processing', 'completed', 'failed')",
            name="ck_purchase_sync_status",
        ),
        CheckConstraint("attempt_count >= 0", name="ck_purchase_sync_attempt_count"),
        Index("ix_purchase_sync_pending", "status", "available_at"),
    )
