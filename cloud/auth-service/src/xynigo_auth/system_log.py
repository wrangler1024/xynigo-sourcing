from __future__ import annotations

import hashlib
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import delete, func, or_, select
from sqlalchemy.orm import Session

from .business_log import sanitize_log_value
from .models import SystemLogEvent


SYSTEM_RUNTIME_CATEGORY = "system_runtime"
SYSTEM_ERROR_CATEGORY = "system_error"
SYSTEM_LOG_LEVELS = frozenset({"info", "warning", "error", "critical"})
SYSTEM_LOG_CATEGORIES = frozenset({SYSTEM_RUNTIME_CATEGORY, SYSTEM_ERROR_CATEGORY})
SYSTEM_LOG_EXCLUDED_ROUTES = frozenset(
    {"/healthz", "/readyz", "/v1/system-logs"}
)

_SAFE_IDENTIFIER = re.compile(r"^[A-Za-z0-9._:-]{1,160}$")
_SAFE_HTTP_METHOD = re.compile(r"^[A-Z]{1,16}$")


def _safe_text(value: object, *, limit: int) -> str:
    sanitized = sanitize_log_value(value)
    if not isinstance(sanitized, str):
        sanitized = str(sanitized or "")
    return " ".join(sanitized.split())[:limit]


def _identifier(value: object, *, fallback: str, limit: int = 160) -> str:
    normalized = str(value or "").strip()[:limit]
    return normalized if _SAFE_IDENTIFIER.fullmatch(normalized) else fallback


def normalize_route(value: object) -> str:
    """Keep only a bounded path/template; query strings and fragments are discarded."""
    route = str(value or "").partition("?")[0].partition("#")[0]
    route = "".join(char for char in route if char >= " " and char != "\x7f")
    return route[:255] or "/unknown"


def should_capture_http(
    *,
    method: str,
    route: str,
    status_code: int,
    trace_id: str,
    runtime_sample_rate: float,
) -> bool:
    normalized_route = normalize_route(route)
    if normalized_route in SYSTEM_LOG_EXCLUDED_ROUTES or normalized_route.startswith(
        "/v1/system-logs/"
    ):
        return False
    if status_code >= 400 or method.upper() not in {"GET", "HEAD", "OPTIONS"}:
        return True
    rate = min(1.0, max(0.0, float(runtime_sample_rate)))
    if rate >= 1.0:
        return True
    if rate <= 0.0:
        return False
    bucket = int(hashlib.sha256(trace_id.encode("utf-8")).hexdigest()[:8], 16)
    return bucket / 0xFFFFFFFF < rate


def _fingerprint(
    *,
    service: str,
    component: str,
    event_type: str,
    level: str,
    route: str | None,
    status_code: int | None,
    exception_type: str | None,
    error_code: str | None,
) -> str:
    canonical = "|".join(
        (
            service,
            component,
            event_type,
            level,
            route or "",
            str(status_code or ""),
            exception_type or "",
            error_code or "",
        )
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class SystemLogService:
    """Append, query and internally retire bounded structured system logs."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def append(
        self,
        *,
        tenant_id: uuid.UUID | None,
        actor_user_id: uuid.UUID | None,
        actor_name: str | None,
        category: str,
        level: str,
        service: str,
        component: str,
        environment: str,
        event_type: str,
        message: str,
        request_id: str,
        trace_id: str,
        retention_days: int,
        source: str = "api",
        client_version: str | None = None,
        http_method: str | None = None,
        route: str | None = None,
        status_code: int | None = None,
        duration_ms: int | None = None,
        exception_type: str | None = None,
        error_code: str | None = None,
        details: dict[str, Any] | None = None,
        operation_time: datetime | None = None,
    ) -> SystemLogEvent:
        normalized_level = level if level in SYSTEM_LOG_LEVELS else "error"
        normalized_category = (
            category if category in SYSTEM_LOG_CATEGORIES else SYSTEM_ERROR_CATEGORY
        )
        normalized_service = _identifier(service, fallback="unknown_service", limit=64)
        normalized_component = _identifier(component, fallback="unknown_component", limit=64)
        normalized_event_type = _identifier(
            event_type, fallback="system.unknown", limit=160
        )
        normalized_route = normalize_route(route) if route else None
        normalized_exception_type = (
            _identifier(exception_type, fallback="UnknownError", limit=160)
            if exception_type
            else None
        )
        normalized_error_code = (
            _identifier(error_code, fallback="system_error", limit=160)
            if error_code
            else None
        )
        normalized_method = str(http_method or "").upper()
        if not _SAFE_HTTP_METHOD.fullmatch(normalized_method):
            normalized_method = None
        now = operation_time or datetime.now(timezone.utc)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        safe_details = sanitize_log_value(details or {})
        assert isinstance(safe_details, dict)
        event = SystemLogEvent(
            tenant_id=tenant_id,
            actor_user_id=actor_user_id,
            actor_name=_safe_text(actor_name, limit=255) if actor_name else None,
            category=normalized_category,
            level=normalized_level,
            service=normalized_service,
            component=normalized_component,
            environment=_identifier(environment, fallback="unknown", limit=32),
            event_type=normalized_event_type,
            message=_safe_text(message, limit=500) or "System event",
            http_method=normalized_method,
            route=normalized_route,
            status_code=max(100, min(599, int(status_code)))
            if status_code is not None
            else None,
            duration_ms=max(0, min(86_400_000, int(duration_ms)))
            if duration_ms is not None
            else None,
            exception_type=normalized_exception_type,
            error_code=normalized_error_code,
            fingerprint=_fingerprint(
                service=normalized_service,
                component=normalized_component,
                event_type=normalized_event_type,
                level=normalized_level,
                route=normalized_route,
                status_code=status_code,
                exception_type=normalized_exception_type,
                error_code=normalized_error_code,
            ),
            source=_identifier(source, fallback="api", limit=64),
            client_version=_safe_text(client_version, limit=64)
            if client_version
            else None,
            request_id=_identifier(request_id, fallback=uuid.uuid4().hex, limit=64),
            trace_id=_identifier(trace_id, fallback=uuid.uuid4().hex, limit=64),
            details=safe_details,
            created_at=now,
            expires_at=now + timedelta(days=max(1, min(365, int(retention_days)))),
        )
        self.session.add(event)
        return event

    def enforce_retention(
        self,
        *,
        retention_days: int,
        max_rows_per_tenant: int,
        now: datetime | None = None,
    ) -> dict[str, int]:
        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        retention_threshold = current - timedelta(
            days=max(1, min(365, int(retention_days)))
        )
        expired = int(
            self.session.execute(
                delete(SystemLogEvent).where(
                    or_(
                        SystemLogEvent.expires_at <= current,
                        SystemLogEvent.created_at < retention_threshold,
                    )
                )
            ).rowcount
            or 0
        )
        capacity_deleted = 0
        limit = max(1, int(max_rows_per_tenant))
        tenant_ids = list(
            self.session.scalars(
                select(SystemLogEvent.tenant_id)
                .where(SystemLogEvent.tenant_id.is_not(None))
                .distinct()
            )
        )
        for tenant_id in tenant_ids:
            count = int(
                self.session.scalar(
                    select(func.count(SystemLogEvent.id)).where(
                        SystemLogEvent.tenant_id == tenant_id
                    )
                )
                or 0
            )
            excess = count - limit
            if excess <= 0:
                continue
            oldest_ids = select(SystemLogEvent.id).where(
                SystemLogEvent.tenant_id == tenant_id
            ).order_by(
                SystemLogEvent.created_at.asc(), SystemLogEvent.id.asc()
            ).limit(excess)
            capacity_deleted += int(
                self.session.execute(
                    delete(SystemLogEvent).where(SystemLogEvent.id.in_(oldest_ids))
                ).rowcount
                or 0
            )
        return {"expired": expired, "capacity": capacity_deleted}

    def list_events(
        self,
        *,
        tenant_id: uuid.UUID,
        started_at: datetime | None = None,
        ended_at: datetime | None = None,
        category: str | None = None,
        level: str | None = None,
        service: str | None = None,
        component: str | None = None,
        event_type: str | None = None,
        status_code: int | None = None,
        request_id: str | None = None,
        keyword: str | None = None,
        page: int = 1,
        page_size: int = 50,
    ) -> dict[str, Any]:
        filters = [SystemLogEvent.tenant_id == tenant_id]
        if started_at is not None:
            filters.append(SystemLogEvent.created_at >= started_at)
        if ended_at is not None:
            filters.append(SystemLogEvent.created_at <= ended_at)
        if category:
            filters.append(SystemLogEvent.category == category)
        if level:
            filters.append(SystemLogEvent.level == level)
        if service:
            filters.append(SystemLogEvent.service == service)
        if component:
            filters.append(SystemLogEvent.component == component)
        if event_type:
            filters.append(SystemLogEvent.event_type.ilike(f"%{event_type.strip()}%"))
        if status_code is not None:
            filters.append(SystemLogEvent.status_code == status_code)
        if request_id:
            pattern = f"%{request_id.strip()}%"
            filters.append(
                or_(
                    SystemLogEvent.request_id.ilike(pattern),
                    SystemLogEvent.trace_id.ilike(pattern),
                )
            )
        if keyword:
            pattern = f"%{keyword.strip()}%"
            filters.append(
                or_(
                    SystemLogEvent.message.ilike(pattern),
                    SystemLogEvent.event_type.ilike(pattern),
                    SystemLogEvent.route.ilike(pattern),
                    SystemLogEvent.error_code.ilike(pattern),
                    SystemLogEvent.fingerprint.ilike(pattern),
                )
            )
        total = int(
            self.session.scalar(select(func.count(SystemLogEvent.id)).where(*filters))
            or 0
        )
        records = list(
            self.session.scalars(
                select(SystemLogEvent)
                .where(*filters)
                .order_by(SystemLogEvent.created_at.desc(), SystemLogEvent.id.desc())
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
        )
        return {
            "scope": "tenant",
            "page": page,
            "pageSize": page_size,
            "total": total,
            "items": [self.serialize(record, include_details=False) for record in records],
        }

    def get_event(
        self, *, tenant_id: uuid.UUID, event_id: uuid.UUID
    ) -> dict[str, Any] | None:
        record = self.session.scalar(
            select(SystemLogEvent).where(
                SystemLogEvent.id == event_id,
                SystemLogEvent.tenant_id == tenant_id,
            )
        )
        return self.serialize(record, include_details=True) if record else None

    @staticmethod
    def serialize(record: SystemLogEvent, *, include_details: bool) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "systemLogId": str(record.id),
            "tenantId": str(record.tenant_id) if record.tenant_id else None,
            "actor": {
                "id": str(record.actor_user_id) if record.actor_user_id else None,
                "name": record.actor_name or "系统",
            },
            "category": record.category,
            "level": record.level,
            "service": record.service,
            "component": record.component,
            "environment": record.environment,
            "eventType": record.event_type,
            "message": record.message,
            "http": {
                "method": record.http_method,
                "route": record.route,
                "statusCode": record.status_code,
                "durationMs": record.duration_ms,
            },
            "error": {
                "type": record.exception_type,
                "code": record.error_code,
                "fingerprint": record.fingerprint,
            },
            "source": record.source,
            "clientVersion": record.client_version,
            "requestId": record.request_id,
            "traceId": record.trace_id,
            "operationTime": record.created_at.isoformat(),
            "expiresAt": record.expires_at.isoformat(),
        }
        if include_details:
            payload["details"] = record.details or {}
        return payload
