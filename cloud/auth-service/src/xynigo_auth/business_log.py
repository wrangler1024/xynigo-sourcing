"""业务/安全日志：统一事件名、脱敏后写入 audit_events。

新接口不要自己拼日志字段，从 BUSINESS_EVENT_CATALOG 选一个 action。
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from .models import AuditEvent, Role, User, UserRole


BUSINESS_LOG_CATEGORY = "business_operation"
SECURITY_LOG_CATEGORY = "security"
SYSTEM_RUNTIME_LOG_CATEGORY = "system_runtime"
SYSTEM_ERROR_LOG_CATEGORY = "system_error"


@dataclass(frozen=True)
class BusinessLogDescriptor:
    """一条业务日志的模块、操作类型、对象类型。"""
    module: str
    operation_type: str
    business_object_type: str | None = None
    category: str = BUSINESS_LOG_CATEGORY


# This is the single taxonomy for business operation logs. Future procurement
# endpoints must select an event from this catalog instead of inventing their
# own payload shape at the call site.
BUSINESS_EVENT_CATALOG: dict[str, BusinessLogDescriptor] = {
    "purchase_order.draft.save": BusinessLogDescriptor(
        "procurement", "purchase_order.draft.save", "purchase_order"
    ),
    "purchase_order.submit": BusinessLogDescriptor(
        "procurement", "purchase_order.submit", "purchase_order"
    ),
    "purchase_order.read": BusinessLogDescriptor(
        "procurement", "purchase_order.read", "purchase_order"
    ),
    "purchase_order.workspace.detail.read": BusinessLogDescriptor(
        "procurement", "purchase_order.recipient_sensitive.read", "purchase_order"
    ),
    "purchase_order.lines.claim": BusinessLogDescriptor(
        "procurement", "purchase_order.claim", "purchase_order"
    ),
    "purchase_order.lines.return": BusinessLogDescriptor(
        "procurement", "purchase_order.return_to_task", "purchase_order"
    ),
    "purchase_order.lines.reassign": BusinessLogDescriptor(
        "procurement", "purchase_order.reassign", "purchase_order"
    ),
    "purchase_order.split_plan.save": BusinessLogDescriptor(
        "procurement", "purchase_order.order_plan.modify", "purchase_order"
    ),
    "purchase_order.order_plan.confirm": BusinessLogDescriptor(
        "procurement", "purchase_order.order_plan.confirm", "order_plan"
    ),
    "purchase_order.order_plan.abandon": BusinessLogDescriptor(
        "procurement", "purchase_order.order_plan.abandon", "order_plan"
    ),
    "purchase_order.resource.bind": BusinessLogDescriptor(
        "procurement", "purchase_order.resource.bind", "resource_binding"
    ),
    "purchase_order.environment.open": BusinessLogDescriptor(
        "procurement", "purchase_order.environment.open", "hub_environment"
    ),
    "purchase_order.payment.success": BusinessLogDescriptor(
        "procurement", "purchase_order.payment.success", "payment"
    ),
    "purchase_order.payment.failure": BusinessLogDescriptor(
        "procurement", "purchase_order.payment.failure", "payment"
    ),
    "purchase_order.batch.create": BusinessLogDescriptor(
        "procurement", "purchase_order.batch.create", "purchase_batch"
    ),
    "purchase_order.buyer_account.release": BusinessLogDescriptor(
        "procurement", "purchase_order.buyer_account.release", "buyer_account"
    ),
    "purchase_order.environment.delete_abandoned": BusinessLogDescriptor(
        "procurement", "purchase_order.environment.delete_abandoned", "hub_environment"
    ),
    "purchase_order.paid_environment.order_lookup.open": BusinessLogDescriptor(
        "procurement",
        "purchase_order.paid_environment.order_lookup.open",
        "hub_environment",
    ),
    "purchase_order.follow_up.update": BusinessLogDescriptor(
        "procurement", "purchase_order.follow_up.update", "purchase_order"
    ),
    "purchase_order.exception.handle": BusinessLogDescriptor(
        "procurement", "purchase_order.exception.handle", "purchase_exception"
    ),
    "purchase_order.platform_order_no.fill": BusinessLogDescriptor(
        "procurement", "purchase_order.platform_order_no.fill", "purchase_batch"
    ),
}


_SENSITIVE_KEY = re.compile(
    r"(?:password|passwd|pwd|cookie|token|secret|api[_-]?key|authorization|"
    r"credential|open[_-]?id|union[_-]?id|phone|mobile|telephone|address|"
    r"postal|zipcode|receiver|recipient.*(?:phone|address)|proxy.*(?:user|pass))",
    re.IGNORECASE,
)
_EMAIL = re.compile(r"(?<![\w.+-])([\w.+-]{1,64})@([\w.-]{1,190})(?![\w.-])")
_INLINE_SECRET = re.compile(
    r"(?i)\b(?:bearer|token|api[_-]?key|secret|password|cookie)\b\s*[:=]?\s*\S+"
)
_SAFE_IDENTIFIER = re.compile(r"^[A-Za-z0-9._:-]{1,160}$")


def _redact_email(match: re.Match[str]) -> str:
    local = match.group(1)
    domain = match.group(2)
    return f"{local[:1]}***@{domain}"


def _sanitize_text(value: object, *, limit: int = 1000) -> str:
    text = " ".join(str(value or "").split())
    if text.startswith(("http://", "https://")):
        try:
            parsed = urlsplit(text)
            text = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
        except ValueError:
            text = "[REDACTED_URL]"
    text = _EMAIL.sub(_redact_email, text)
    text = _INLINE_SECRET.sub("[REDACTED]", text)
    return text[:limit]


def sanitize_log_value(value: Any, *, key: str = "", depth: int = 0) -> Any:
    """Return a bounded, JSON-safe and field-level redacted log value."""
    if _SENSITIVE_KEY.search(str(key or "")):
        return "[REDACTED]"
    if depth >= 6:
        return "[TRUNCATED]"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, str):
        return _sanitize_text(value)
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for index, (raw_key, item) in enumerate(value.items()):
            if index >= 100:
                result["_truncated"] = True
                break
            safe_key = _sanitize_text(raw_key, limit=120)
            result[safe_key] = sanitize_log_value(
                item, key=safe_key, depth=depth + 1
            )
        return result
    if isinstance(value, (list, tuple, set, frozenset)):
        items = list(value)
        result = [
            sanitize_log_value(item, key=key, depth=depth + 1)
            for item in items[:100]
        ]
        if len(items) > 100:
            result.append("[TRUNCATED]")
        return result
    return _sanitize_text(value)


def _identifier(value: str | None, *, fallback: str = "") -> str:
    normalized = str(value or "").strip()
    if not _SAFE_IDENTIFIER.fullmatch(normalized):
        return fallback
    return normalized


def _descriptor_for(action: str) -> BusinessLogDescriptor:
    descriptor = BUSINESS_EVENT_CATALOG.get(action)
    if descriptor is not None:
        return descriptor
    if action.startswith("auth."):
        return BusinessLogDescriptor("auth", action, category=SECURITY_LOG_CATEGORY)
    if action.startswith("admin."):
        return BusinessLogDescriptor("system", action, "administration")
    if action.startswith("purchase") or action.startswith("procurement"):
        return BusinessLogDescriptor("procurement", action, "purchase_order")
    prefix = action.partition(".")[0]
    return BusinessLogDescriptor(prefix or "system", action)


def _outcome_for(result: str, details: dict[str, Any], explicit: str | None) -> str:
    if explicit:
        return _identifier(explicit, fallback="failure")
    if result == "success":
        return "success"
    reason = str(details.get("reason") or "")
    if details.get("permission") or reason == "permission_denied":
        return "permission_denied"
    if "not_found" in reason or "cross_tenant" in reason:
        return "not_found"
    if "conflict" in reason or "locked" in reason or "started" in reason:
        return "business_conflict"
    if result == "denied":
        return "validation_failed"
    return "failure"


class BusinessLogService:
    """把一次 API 操作记入审计表；敏感字段在 sanitize_log_value 里打码。"""
    def __init__(self, session: Session) -> None:
        self.session = session

    def append(
        self,
        *,
        tenant_id: uuid.UUID | None,
        actor_user_id: uuid.UUID | None,
        action: str,
        result: str,
        request_id: str,
        trace_id: str | None = None,
        outcome: str | None = None,
        module: str | None = None,
        category: str | None = None,
        business_object_type: str | None = None,
        business_object_id: str | uuid.UUID | None = None,
        business_object_no: str | None = None,
        failure_reason: str | None = None,
        change_summary: dict[str, Any] | None = None,
        source: str = "api",
        client_version: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> AuditEvent:
        """追加一条审计。action 必须是目录里的名字，或显式传入 module/category。"""
        action = _identifier(action, fallback="system.unknown")
        descriptor = _descriptor_for(action)
        sanitized_details = sanitize_log_value(details or {})
        assert isinstance(sanitized_details, dict)
        actor_name = None
        actor_roles: list[str] = []
        if actor_user_id is not None:
            actor = self.session.get(User, actor_user_id)
            if actor is not None and (tenant_id is None or actor.tenant_id == tenant_id):
                actor_name = _sanitize_text(actor.display_name, limit=255)
                actor_roles = list(
                    self.session.scalars(
                        select(Role.code)
                        .join(UserRole, UserRole.role_id == Role.id)
                        .where(UserRole.user_id == actor_user_id)
                        .order_by(Role.code)
                    )
                )
        event = AuditEvent(
            tenant_id=tenant_id,
            actor_user_id=actor_user_id,
            actor_name=actor_name,
            actor_roles=actor_roles,
            category=_identifier(category, fallback=descriptor.category)
            if category
            else descriptor.category,
            module=_identifier(module, fallback=descriptor.module)
            if module
            else descriptor.module,
            action=action,
            operation_type=descriptor.operation_type,
            result=result,
            outcome=_outcome_for(result, sanitized_details, outcome),
            business_object_type=_sanitize_text(
                business_object_type or descriptor.business_object_type or "", limit=64
            )
            or None,
            business_object_id=_sanitize_text(business_object_id, limit=160)
            if business_object_id is not None
            else None,
            business_object_no=_sanitize_text(business_object_no, limit=255)
            if business_object_no
            else None,
            failure_reason=_sanitize_text(failure_reason, limit=160)
            if failure_reason
            else None,
            change_summary=sanitize_log_value(change_summary or {}),
            source=_identifier(source, fallback="api"),
            client_version=_sanitize_text(client_version, limit=64)
            if client_version
            else None,
            request_id=_identifier(request_id, fallback=uuid.uuid4().hex),
            trace_id=_identifier(trace_id, fallback="")
            or _identifier(request_id, fallback=uuid.uuid4().hex),
            details=sanitized_details,
        )
        self.session.add(event)
        return event

    def list_events(
        self,
        *,
        tenant_id: uuid.UUID,
        viewer_user_id: uuid.UUID,
        tenant_wide: bool,
        started_at: datetime | None = None,
        ended_at: datetime | None = None,
        module: str | None = None,
        operator: str | None = None,
        outcome: str | None = None,
        business_no: str | None = None,
        operation_type: str | None = None,
        request_id: str | None = None,
        page: int = 1,
        page_size: int = 50,
    ) -> dict[str, Any]:
        filters = [
            AuditEvent.tenant_id == tenant_id,
            AuditEvent.category == BUSINESS_LOG_CATEGORY,
        ]
        if not tenant_wide:
            filters.append(AuditEvent.actor_user_id == viewer_user_id)
        if started_at is not None:
            filters.append(AuditEvent.created_at >= started_at)
        if ended_at is not None:
            filters.append(AuditEvent.created_at <= ended_at)
        if module:
            filters.append(AuditEvent.module == module)
        normalized_operator = str(operator or "").strip()
        if normalized_operator:
            operator_filters = [AuditEvent.actor_name.ilike(f"%{normalized_operator}%")]
            try:
                operator_filters.append(
                    AuditEvent.actor_user_id == uuid.UUID(normalized_operator)
                )
            except ValueError:
                pass
            filters.append(or_(*operator_filters))
        if outcome:
            filters.append(AuditEvent.outcome == outcome)
        if business_no:
            pattern = f"%{str(business_no).strip()}%"
            filters.append(
                or_(
                    AuditEvent.business_object_no.ilike(pattern),
                    AuditEvent.business_object_id.ilike(pattern),
                )
            )
        if operation_type:
            filters.append(AuditEvent.operation_type.ilike(f"%{operation_type.strip()}%"))
        if request_id:
            pattern = f"%{request_id.strip()}%"
            filters.append(
                or_(AuditEvent.request_id.ilike(pattern), AuditEvent.trace_id.ilike(pattern))
            )
        total = int(
            self.session.scalar(
                select(func.count(AuditEvent.id)).where(*filters)
            )
            or 0
        )
        records = list(
            self.session.scalars(
                select(AuditEvent)
                .where(*filters)
                .order_by(AuditEvent.created_at.desc(), AuditEvent.id.desc())
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
        )
        return {
            "scope": "tenant" if tenant_wide else "self",
            "page": page,
            "pageSize": page_size,
            "total": total,
            "items": [self.serialize(record, include_details=False) for record in records],
        }

    def get_event(
        self,
        *,
        tenant_id: uuid.UUID,
        viewer_user_id: uuid.UUID,
        event_id: uuid.UUID,
        tenant_wide: bool,
    ) -> dict[str, Any] | None:
        filters = [
            AuditEvent.id == event_id,
            AuditEvent.tenant_id == tenant_id,
            AuditEvent.category == BUSINESS_LOG_CATEGORY,
        ]
        if not tenant_wide:
            filters.append(AuditEvent.actor_user_id == viewer_user_id)
        record = self.session.scalar(select(AuditEvent).where(*filters))
        return self.serialize(record, include_details=True) if record is not None else None

    @staticmethod
    def serialize(record: AuditEvent, *, include_details: bool) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "businessLogId": str(record.id),
            "tenantId": str(record.tenant_id) if record.tenant_id else None,
            "operator": {
                "id": str(record.actor_user_id) if record.actor_user_id else None,
                "name": record.actor_name or "系统",
                "roles": list(record.actor_roles or []),
            },
            "module": record.module,
            "operationType": record.operation_type,
            "businessObject": {
                "type": record.business_object_type,
                "id": record.business_object_id,
                "number": record.business_object_no,
            },
            "result": record.outcome,
            "resultGroup": record.result,
            "failureReason": record.failure_reason,
            "changeSummary": record.change_summary or {},
            "source": record.source,
            "clientVersion": record.client_version,
            "requestId": record.request_id,
            "traceId": record.trace_id,
            "operationTime": record.created_at.isoformat(),
        }
        if include_details:
            payload["details"] = record.details or {}
        return payload
