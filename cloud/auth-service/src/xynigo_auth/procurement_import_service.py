"""Durable cloud orchestration for XYP2 collaboration-sheet imports."""

from __future__ import annotations

import base64
import hashlib
import logging
import threading
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import and_, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from .models import ProcurementImportJob, ProcurementImportPlan, User
from .procurement_import_core import (
    CollaborationRow,
    CollaborationSheetTarget,
    ImageSyncJob,
    ImportPlan,
    ProcurementImportError,
    ProcurementImportService,
    require_importable,
)
from .procurement_import_crypto import (
    ProcurementImportCipher,
    ProcurementImportCipherError,
)
from .procurement_import_sheet import FeishuSheetsGateway, LarkSheetSyncError
from .procurement_import_preferences import touch_target

logger = logging.getLogger(__name__)
NONTERMINAL_STATES = frozenset(
    {
        "queued",
        "validating",
        "normalizing_headers",
        "formatting_headers",
        "writing_rows",
        "verifying_rows",
        "formatting_rows",
        "writing_links",
        "writing_images",
    }
)
TERMINAL_STATES = frozenset({"completed", "partial", "failed"})


def utcnow() -> datetime:
    return datetime.now(UTC)


def _as_aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


class CloudProcurementImportError(RuntimeError):
    def __init__(self, code: str, message: str, *, status: int = 422,
                 diagnostics: dict[str, Any] | None = None) -> None:
        self.code = code
        self.status = status
        self.diagnostics = diagnostics
        super().__init__(message)


def _plan_payload(plan: ImportPlan) -> dict[str, Any]:
    target = None
    if plan.target is not None:
        target = {
            "url": str(plan.target.url),
            "sheetId": str(plan.target.sheet_id),
            "sheetName": str(plan.target.sheet_name),
            "revision": plan.target.revision,
            "validatedAt": float(plan.target.validated_at),
        }
    return {
        "version": 1,
        "filename": str(plan.filename),
        "issues": list(plan.issues),
        "allowPartial": plan.allow_partial,
        "sourceRows": int(plan.source_rows),
        "orderCount": int(plan.order_count),
        "importBatch": str(plan.import_batch),
        "createdAt": float(plan.created_at),
        "target": target,
        "rows": [
            {
                "values": dict(row.values),
                "orderImage": base64.b64encode(bytes(row.order_image or b"")).decode(
                    "ascii"
                ),
                "orderImageUrl": str(row.order_image_url or ""),
                "purchaseCurrency": str(row.purchase_currency or ""),
                "salesCurrency": str(row.sales_currency or ""),
                "itemSalesAmount": row.item_sales_amount,
                "orderGroupIndex": int(row.order_group_index),
                "sourceUnmatched": row.source_unmatched,
            }
            for row in plan.rows
        ],
    }


def _plan_from_payload(plan_id: object, payload: dict[str, Any]) -> ImportPlan:
    if payload.get("version") != 1 or not isinstance(payload.get("rows"), list):
        raise CloudProcurementImportError(
            "procurement_import_plan_invalid",
            "采购协作导入计划结构无效，请重新解析",
            status=409,
        )
    rows = []
    try:
        for item in payload["rows"]:
            rows.append(
                CollaborationRow(
                    values=dict(item["values"]),
                    order_image=base64.b64decode(
                        str(item.get("orderImage") or ""), validate=True
                    ),
                    order_image_url=str(item.get("orderImageUrl") or ""),
                    purchase_currency=str(item.get("purchaseCurrency") or ""),
                    sales_currency=str(item.get("salesCurrency") or ""),
                    item_sales_amount=item.get("itemSalesAmount"),
                    order_group_index=int(item.get("orderGroupIndex") or 0),
                    source_unmatched=item.get("sourceUnmatched") is True,
                )
            )
        plan = ImportPlan(
            plan_id=str(plan_id),
            filename=str(payload["filename"]),
            rows=rows,
            issues=list(payload.get("issues") or []),
            source_rows=int(payload.get("sourceRows") or 0),
            order_count=int(payload.get("orderCount") or 0),
            import_batch=str(payload["importBatch"]),
            created_at=float(payload.get("createdAt") or time.time()),
            allow_partial=payload.get("allowPartial") is True,
        )
        target = payload.get("target")
        if isinstance(target, dict):
            plan.target = CollaborationSheetTarget(
                url=str(target["url"]),
                sheet_id=str(target["sheetId"]),
                sheet_name=str(target["sheetName"]),
                revision=target.get("revision"),
                validated_at=float(target.get("validatedAt") or time.time()),
            )
        return plan
    except CloudProcurementImportError:
        raise
    except Exception as exc:
        raise CloudProcurementImportError(
            "procurement_import_plan_invalid",
            "采购协作导入计划结构无效，请重新解析",
            status=409,
        ) from exc


def _safe_progress(progress: dict[str, Any]) -> dict[str, Any]:
    result = dict(progress)
    # Row-specific errors may contain order identifiers.  They remain in the
    # encrypted plan and live response only; durable progress stores counters.
    result["errors"] = []
    result["error"] = str(result.get("error") or "")[:300]
    result["targetName"] = str(result.get("targetName") or "")[:100]
    # The collaboration-sheet URL is the one the operator pasted for this
    # import; it is kept so history can reopen the target later.
    result["targetUrl"] = str(result.get("targetUrl") or "")[:1024]
    return result


class CloudProcurementImportService:
    def __init__(
        self,
        *,
        session_factory: sessionmaker[Session],
        gateway: FeishuSheetsGateway,
        cipher: ProcurementImportCipher,
        plan_ttl_seconds: int = 1800,
        max_active_plans_per_tenant: int = 5,
    ) -> None:
        self.session_factory = session_factory
        self.gateway = gateway
        self.cipher = cipher
        self.plan_ttl_seconds = int(plan_ttl_seconds)
        self.max_active_plans_per_tenant = int(max_active_plans_per_tenant)

    def expire_plans(self, session: Session) -> int:
        now = utcnow()
        records = list(
            session.scalars(
                select(ProcurementImportPlan).where(
                    ProcurementImportPlan.status != "expired",
                    ProcurementImportPlan.expires_at <= now,
                )
            )
        )
        for record in records:
            record.status = "expired"
            record.encrypted_payload = None
        if records:
            session.flush()
        return len(records)

    @staticmethod
    def _parse_uuid(value: object, *, code: str) -> uuid.UUID:
        try:
            return uuid.UUID(str(value or ""))
        except (ValueError, TypeError, AttributeError) as exc:
            raise CloudProcurementImportError(code, "请求标识格式无效") from exc

    def _record(
        self, session: Session, *, tenant_id: uuid.UUID, plan_id: object
    ) -> ProcurementImportPlan:
        identifier = self._parse_uuid(plan_id, code="procurement_import_plan_invalid")
        record = session.scalar(
            select(ProcurementImportPlan).where(
                ProcurementImportPlan.id == identifier,
                ProcurementImportPlan.tenant_id == tenant_id,
            )
        )
        if record is None:
            raise CloudProcurementImportError(
                "procurement_import_plan_not_found",
                "解析计划不存在或不属于当前组织",
                status=404,
            )
        if record.status == "expired" or _as_aware(record.expires_at) <= utcnow():
            record.status = "expired"
            record.encrypted_payload = None
            session.flush()
            raise CloudProcurementImportError(
                "procurement_import_plan_expired",
                "解析计划已过期，请重新选择 xlsx",
                status=410,
            )
        if not record.encrypted_payload:
            raise CloudProcurementImportError(
                "procurement_import_plan_expired",
                "解析计划已过期，请重新选择 xlsx",
                status=410,
            )
        return record

    def _load_plan(self, record: ProcurementImportPlan) -> ImportPlan:
        try:
            payload = self.cipher.decrypt(
                record.encrypted_payload,
                tenant_id=record.tenant_id,
                plan_id=record.id,
            )
        except ProcurementImportCipherError as exc:
            raise CloudProcurementImportError(
                "procurement_import_plan_decrypt_failed",
                "解析计划无法解密，请重新选择 xlsx",
                status=409,
            ) from exc
        return _plan_from_payload(record.id, payload)

    def _save_plan(
        self, record: ProcurementImportPlan, plan: ImportPlan, *, status: str
    ) -> None:
        payload = _plan_payload(plan)
        record.encrypted_payload = self.cipher.encrypt(
            payload, tenant_id=record.tenant_id, plan_id=record.id
        )
        record.payload_hash = hashlib.sha256(record.encrypted_payload).hexdigest()
        record.status = status

    def _core(self, plan: ImportPlan | None = None) -> ProcurementImportService:
        core = ProcurementImportService(sheet_gateway=self.gateway)
        if plan is not None:
            core.pending[plan.plan_id] = plan
        return core

    def parse(
        self,
        session: Session,
        *,
        tenant_id: uuid.UUID,
        actor_user_id: uuid.UUID,
        filename: str,
        content_base64: str,
        allow_partial: bool = False,
    ) -> dict[str, Any]:
        self.expire_plans(session)
        active_count = int(
            session.scalar(
                select(func.count(ProcurementImportPlan.id)).where(
                    ProcurementImportPlan.tenant_id == tenant_id,
                    ProcurementImportPlan.status != "expired",
                    ProcurementImportPlan.expires_at > utcnow(),
                )
            )
            or 0
        )
        if active_count >= self.max_active_plans_per_tenant:
            raise CloudProcurementImportError(
                "procurement_import_plan_limit",
                "当前组织的短时解析计划过多，请稍后重试",
                status=429,
            )
        try:
            core = self._core()
            result = core.parse(filename, content_base64, allow_partial=allow_partial)
            source_plan = core.pending[result["planId"]]
        except ProcurementImportError as exc:
            raise CloudProcurementImportError(
                "procurement_import_parse_failed", str(exc), status=422,
                diagnostics=exc.diagnostics,
            ) from exc
        identifier = uuid.uuid4()
        source_plan.plan_id = str(identifier)
        payload = _plan_payload(source_plan)
        encrypted = self.cipher.encrypt(
            payload, tenant_id=tenant_id, plan_id=identifier
        )
        record = ProcurementImportPlan(
            id=identifier,
            tenant_id=tenant_id,
            created_by_user_id=actor_user_id,
            filename=str(source_plan.filename)[:255],
            import_batch=str(source_plan.import_batch)[:128],
            payload_hash=hashlib.sha256(encrypted).hexdigest(),
            encrypted_payload=encrypted,
            status="parsed",
            source_row_count=int(source_plan.source_rows),
            order_count=int(source_plan.order_count),
            detail_count=len(source_plan.rows),
            image_count=sum(bool(row.order_image) for row in source_plan.rows),
            expires_at=utcnow() + timedelta(seconds=self.plan_ttl_seconds),
        )
        session.add(record)
        session.flush()
        result["planId"] = str(identifier)
        result["runtime"] = "cloud"
        result["expiresAt"] = record.expires_at.isoformat()
        return result

    def preview_image(
        self, session: Session, *, tenant_id: uuid.UUID, plan_id: object, row: object
    ) -> tuple[bytes, str]:
        record = self._record(session, tenant_id=tenant_id, plan_id=plan_id)
        plan = self._load_plan(record)
        try:
            return self._core(plan).preview_image(plan.plan_id, row)
        except ProcurementImportError as exc:
            raise CloudProcurementImportError(
                "procurement_import_image_unavailable", str(exc), status=404
            ) from exc

    def export(
        self, session: Session, *, tenant_id: uuid.UUID, plan_id: object
    ) -> tuple[bytes, str, str]:
        record = self._record(session, tenant_id=tenant_id, plan_id=plan_id)
        plan = self._load_plan(record)
        try:
            return self._core(plan).export(plan.plan_id)
        except ProcurementImportError as exc:
            raise CloudProcurementImportError(
                "procurement_import_export_failed", str(exc), status=422
            ) from exc

    def inspect_target(
        self,
        session: Session,
        *,
        tenant_id: uuid.UUID,
        plan_id: object,
        spreadsheet_url: str,
    ) -> dict[str, Any]:
        record = self._record(session, tenant_id=tenant_id, plan_id=plan_id)
        plan = self._load_plan(record)
        try:
            return self._core(plan).inspect_target(plan.plan_id, spreadsheet_url)
        except (ProcurementImportError, LarkSheetSyncError) as exc:
            raise CloudProcurementImportError(
                "procurement_import_target_inspect_failed", str(exc), status=422
            ) from exc

    def validate_target(
        self,
        session: Session,
        *,
        tenant_id: uuid.UUID,
        plan_id: object,
        spreadsheet_url: str,
        sheet_id: str,
    ) -> dict[str, Any]:
        record = self._record(session, tenant_id=tenant_id, plan_id=plan_id)
        plan = self._load_plan(record)
        try:
            result = self._core(plan).validate_target(
                plan.plan_id, spreadsheet_url, sheet_id
            )
        except (ProcurementImportError, LarkSheetSyncError) as exc:
            raise CloudProcurementImportError(
                "procurement_import_target_invalid", str(exc), status=422
            ) from exc
        self._save_plan(record, plan, status="validated")
        session.flush()
        return result

    def start_sync(
        self,
        session: Session,
        *,
        tenant_id: uuid.UUID,
        actor_user_id: uuid.UUID,
        actor_name: str,
        plan_id: object,
        confirm_write: bool,
        fill_order_background: bool = True,
    ) -> dict[str, Any]:
        if not confirm_write:
            raise CloudProcurementImportError(
                "procurement_import_confirmation_required",
                "导入前必须明确确认规范协作表、追加本批采购数据并补齐订单商品图",
                status=409,
            )
        record = self._record(session, tenant_id=tenant_id, plan_id=plan_id)
        plan = self._load_plan(record)
        if plan.target is None or record.status != "validated":
            raise CloudProcurementImportError(
                "procurement_import_target_not_validated",
                "请先读取工作表并通过核心采购字段校验",
                status=409,
            )
        try:
            require_importable(plan)
        except ProcurementImportError as exc:
            raise CloudProcurementImportError(
                "procurement_import_blocked", str(exc), status=422,
                diagnostics=exc.diagnostics,
            ) from exc
        actor = " ".join(str(actor_name or "").split())[:100]
        if actor:
            for row in plan.rows:
                row.values["导入操作人"] = actor
            self._save_plan(record, plan, status="validated")
        target_key_hash = hashlib.sha256(
            (
                f"{tenant_id}|{plan.target.url}|{plan.target.sheet_id}|"
                f"{plan.import_batch}"
            ).encode()
        ).hexdigest()
        running = session.scalar(
            select(ProcurementImportJob)
            .where(
                ProcurementImportJob.tenant_id == tenant_id,
                ProcurementImportJob.target_key_hash == target_key_hash,
                ProcurementImportJob.state.in_(NONTERMINAL_STATES),
            )
            .order_by(ProcurementImportJob.created_at.desc())
        )
        if running is not None:
            return dict(running.progress)
        previous = session.scalar(select(ProcurementImportJob).where(
            ProcurementImportJob.tenant_id == tenant_id,
            ProcurementImportJob.target_key_hash == target_key_hash,
        ).order_by(ProcurementImportJob.created_at.desc()).limit(1))
        retry = dict(previous.progress or {}) if previous and previous.state in {'failed', 'partial'} else {}
        identifier = uuid.uuid4()
        progress = ImageSyncJob(
            job_id=str(identifier),
            plan_id=str(record.id),
            target_name=plan.target.sheet_name,
            import_batch=plan.import_batch,
            target_key=target_key_hash,
            rows_total=len(plan.rows),
            fill_order_background=retry.get('fillOrderBackground', fill_order_background),
            background_plan_indices=retry.get('backgroundPlanIndices'),
        ).public()
        job = ProcurementImportJob(
            id=identifier,
            tenant_id=tenant_id,
            plan_id=record.id,
            created_by_user_id=actor_user_id,
            target_key_hash=target_key_hash,
            state="queued",
            progress=_safe_progress(progress),
        )
        session.add(job)
        try:
            session.flush()
        except IntegrityError:
            session.rollback()
            running = session.scalar(
                select(ProcurementImportJob)
                .where(
                    ProcurementImportJob.tenant_id == tenant_id,
                    ProcurementImportJob.target_key_hash == target_key_hash,
                    ProcurementImportJob.state.in_(NONTERMINAL_STATES),
                )
                .order_by(ProcurementImportJob.created_at.desc())
            )
            if running is not None:
                return dict(running.progress)
            raise CloudProcurementImportError(
                "procurement_import_job_conflict",
                "同一目标的导入任务正在创建，请刷新进度后重试",
                status=409,
            )
        return dict(job.progress)

    def status(
        self, session: Session, *, tenant_id: uuid.UUID, job_id: object
    ) -> dict[str, Any]:
        identifier = self._parse_uuid(job_id, code="procurement_import_job_invalid")
        job = session.scalar(
            select(ProcurementImportJob).where(
                ProcurementImportJob.id == identifier,
                ProcurementImportJob.tenant_id == tenant_id,
            )
        )
        if job is None:
            raise CloudProcurementImportError(
                "procurement_import_job_not_found",
                "导入任务不存在或不属于当前组织",
                status=404,
            )
        return dict(job.progress)

    def history_item(
        self,
        job: ProcurementImportJob,
        plan: ProcurementImportPlan | None,
        actor_name: str,
    ) -> dict[str, Any]:
        progress = dict(job.progress or {})
        started = job.started_at
        finished = job.finished_at
        duration = (
            int((finished - started).total_seconds())
            if started is not None and finished is not None and finished >= started
            else None
        )
        return {
            "jobId": str(job.id),
            "state": str(job.state),
            "createdAt": _as_aware(job.created_at).isoformat(),
            "startedAt": _as_aware(started).isoformat() if started else None,
            "finishedAt": _as_aware(finished).isoformat() if finished else None,
            "durationSec": duration,
            "actorUserId": str(job.created_by_user_id),
            "actorDisplayName": actor_name,
            "filename": plan.filename if plan is not None else "",
            "importBatch": plan.import_batch if plan is not None else "",
            "sourceRowCount": plan.source_row_count if plan is not None else 0,
            "orderCount": plan.order_count if plan is not None else 0,
            "detailCount": plan.detail_count if plan is not None else 0,
            "imageCount": plan.image_count if plan is not None else 0,
            "targetName": str(progress.get("targetName") or ""),
            "targetUrl": str(progress.get("targetUrl") or ""),
            "rowsTotal": int(progress.get("rowsTotal") or 0),
            "rowsWritten": int(progress.get("rowsWritten") or 0),
            "rowsExisting": int(progress.get("rowsExisting") or 0),
            "total": int(progress.get("total") or 0),
            "processed": int(progress.get("processed") or 0),
            "written": int(progress.get("written") or 0),
            "skippedExisting": int(progress.get("skippedExisting") or 0),
            "missingSource": int(progress.get("missingSource") or 0),
            "skippedUnmatched": int(progress.get("skippedUnmatched") or 0),
            "failed": int(progress.get("failed") or 0),
            "error": str(progress.get("error") or ""),
        }

    @staticmethod
    def _history_scope(
        tenant_id: uuid.UUID,
        *,
        actor_user_id: uuid.UUID,
        include_all_users: bool,
        filter_actor_user_id: uuid.UUID | None,
    ) -> list[Any]:
        conditions = [ProcurementImportJob.tenant_id == tenant_id]
        if include_all_users:
            if filter_actor_user_id is not None:
                conditions.append(
                    ProcurementImportJob.created_by_user_id == filter_actor_user_id
                )
        else:
            conditions.append(
                ProcurementImportJob.created_by_user_id == actor_user_id
            )
        return conditions

    def history(
        self,
        session: Session,
        *,
        tenant_id: uuid.UUID,
        actor_user_id: uuid.UUID,
        limit: int,
        cursor: object = None,
        status: str | None = None,
        include_all_users: bool = False,
        filter_actor_user_id: uuid.UUID | None = None,
    ) -> dict[str, Any]:
        statement = (
            select(
                ProcurementImportJob,
                ProcurementImportPlan,
                User.display_name,
                User.status,
            )
            .join(
                ProcurementImportPlan,
                ProcurementImportPlan.id == ProcurementImportJob.plan_id,
                isouter=True,
            )
            .join(User, User.id == ProcurementImportJob.created_by_user_id)
            .where(
                ProcurementImportJob.tenant_id == tenant_id,
                *self._history_scope(
                    tenant_id,
                    actor_user_id=actor_user_id,
                    include_all_users=include_all_users,
                    filter_actor_user_id=filter_actor_user_id,
                ),
            )
        )
        if status == "running":
            statement = statement.where(ProcurementImportJob.state.in_(NONTERMINAL_STATES))
        elif status is not None:
            if status not in TERMINAL_STATES:
                raise CloudProcurementImportError(
                    "procurement_import_history_status_invalid",
                    "导入历史筛选状态无效",
                )
            statement = statement.where(ProcurementImportJob.state == status)
        if cursor is not None:
            cursor_uuid = self._parse_uuid(
                cursor, code="procurement_import_history_cursor_invalid"
            )
            # The cursor must pass the same visibility scope as the list:
            # probing foreign job UUIDs must not leak membership either.
            cursor_job = session.scalar(
                select(ProcurementImportJob).where(
                    ProcurementImportJob.id == cursor_uuid,
                    *self._history_scope(
                        tenant_id,
                        actor_user_id=actor_user_id,
                        include_all_users=include_all_users,
                        filter_actor_user_id=filter_actor_user_id,
                    ),
                )
            )
            if cursor_job is None:
                raise CloudProcurementImportError(
                    "procurement_import_history_cursor_invalid",
                    "导入历史游标无效",
                )
            statement = statement.where(
                or_(
                    ProcurementImportJob.created_at < cursor_job.created_at,
                    and_(
                        ProcurementImportJob.created_at == cursor_job.created_at,
                        ProcurementImportJob.id < cursor_job.id,
                    ),
                )
            )
        result_rows = list(
            session.execute(
                statement.order_by(
                    ProcurementImportJob.created_at.desc(),
                    ProcurementImportJob.id.desc(),
                ).limit(limit + 1)
            )
        )
        has_more = len(result_rows) > limit
        page = result_rows[:limit]
        items = [
            self.history_item(job, plan, display_name or "未知用户")
            for job, plan, display_name, _user_status in page
        ]
        actors = []
        if include_all_users:
            actors = [
                {
                    "userId": str(user_id),
                    "displayName": display_name,
                    "status": user_status,
                }
                for user_id, display_name, user_status in session.execute(
                    select(User.id, User.display_name, User.status)
                    .join(
                        ProcurementImportJob,
                        ProcurementImportJob.created_by_user_id == User.id,
                    )
                    .where(
                        User.tenant_id == tenant_id,
                        ProcurementImportJob.tenant_id == tenant_id,
                    )
                    .distinct()
                    .order_by(User.display_name, User.id)
                )
            ]
        return {
            "items": items,
            "nextCursor": str(page[-1][0].id) if has_more and page else None,
            "hasMore": has_more,
            "actors": actors,
        }

    def history_detail(
        self,
        session: Session,
        *,
        tenant_id: uuid.UUID,
        actor_user_id: uuid.UUID,
        job_id: object,
        allow_tenant_scope: bool = False,
    ) -> dict[str, Any]:
        identifier = self._parse_uuid(
            job_id, code="procurement_import_history_job_invalid"
        )
        job = session.scalar(
            select(ProcurementImportJob).where(
                ProcurementImportJob.id == identifier,
                ProcurementImportJob.tenant_id == tenant_id,
            )
        )
        if job is None:
            raise CloudProcurementImportError(
                "procurement_import_history_job_not_found",
                "导入历史记录不存在或不属于当前组织",
                status=404,
            )
        if not allow_tenant_scope and job.created_by_user_id != actor_user_id:
            raise CloudProcurementImportError(
                "procurement_import_history_forbidden",
                "只能查看本人创建的导入历史",
                status=403,
            )
        plan = session.get(ProcurementImportPlan, job.plan_id)
        actor = session.get(User, job.created_by_user_id)
        item = self.history_item(
            job, plan, actor.display_name if actor else "未知用户"
        )
        item["own"] = job.created_by_user_id == actor_user_id
        return item


class _PersistentCoreService(ProcurementImportService):
    def __init__(self, *, gateway: FeishuSheetsGateway, callback) -> None:
        super().__init__(sheet_gateway=gateway)
        self._progress_callback = callback

    def _job_update(self, job_id, **changes):
        job = super()._job_update(job_id, **changes)
        if job is not None and changes:
            self._progress_callback(job.public())
        return job


class ProcurementImportWorker:
    def __init__(
        self,
        *,
        service: CloudProcurementImportService,
        interval_seconds: int = 2,
    ) -> None:
        self.service = service
        self.interval_seconds = max(1, int(interval_seconds))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._recover()
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop,
            name="xynigo-procurement-import-worker",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=max(2.0, self.interval_seconds + 1.0))

    def _recover(self) -> None:
        with self.service.session_factory() as session:
            jobs = list(
                session.scalars(
                    select(ProcurementImportJob).where(
                        ProcurementImportJob.state.in_(NONTERMINAL_STATES - {"queued"})
                    )
                )
            )
            for job in jobs:
                job.state = "queued"
                progress = dict(job.progress or {})
                progress["state"] = "queued"
                progress["error"] = ""
                job.progress = _safe_progress(progress)
            self.service.expire_plans(session)
            session.commit()

    def _loop(self) -> None:
        while not self._stop.is_set():
            job_id = self._claim()
            if job_id is None:
                self._stop.wait(self.interval_seconds)
                continue
            self._run(job_id)

    def _claim(self) -> uuid.UUID | None:
        with self.service.session_factory() as session:
            self.service.expire_plans(session)
            job = session.scalar(
                select(ProcurementImportJob)
                .where(ProcurementImportJob.state == "queued")
                .order_by(ProcurementImportJob.created_at)
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            if job is None:
                session.commit()
                return None
            job.state = "validating"
            job.started_at = job.started_at or utcnow()
            progress = dict(job.progress or {})
            progress["state"] = "validating"
            job.progress = _safe_progress(progress)
            identifier = job.id
            session.commit()
            return identifier

    def _persist(self, job_id: uuid.UUID, progress: dict[str, Any]) -> None:
        with self.service.session_factory() as session:
            job = session.get(ProcurementImportJob, job_id)
            if job is None:
                return
            safe = _safe_progress(progress)
            state = str(safe.get("state") or "failed")
            job.state = state if state in NONTERMINAL_STATES | TERMINAL_STATES else "failed"
            safe["state"] = job.state
            job.progress = safe
            if job.state in TERMINAL_STATES:
                job.finished_at = utcnow()
                job.last_error_code = (
                    None if job.state == "completed" else "procurement_import_sync_failed"
                )
            session.commit()

    def _fail(self, job_id: uuid.UUID, code: str, message: str) -> None:
        with self.service.session_factory() as session:
            job = session.get(ProcurementImportJob, job_id)
            if job is None:
                return
            progress = dict(job.progress or {})
            progress.update(
                {
                    "state": "failed",
                    "error": str(message)[:300],
                    "finishedAt": time.time(),
                }
            )
            job.state = "failed"
            job.progress = _safe_progress(progress)
            job.last_error_code = str(code)[:128]
            job.finished_at = utcnow()
            session.commit()

    def _run(self, job_id: uuid.UUID) -> None:
        try:
            with self.service.session_factory() as session:
                job = session.get(ProcurementImportJob, job_id)
                if job is None:
                    return
                record = session.get(ProcurementImportPlan, job.plan_id)
                if record is None or record.encrypted_payload is None:
                    self._fail(
                        job_id,
                        "procurement_import_plan_expired",
                        "解析计划已过期，请重新选择 xlsx",
                    )
                    return
                plan = self.service._load_plan(record)
                if plan.target is None:
                    self._fail(
                        job_id,
                        "procurement_import_target_not_validated",
                        "目标工作表未通过校验",
                    )
                    return
                initial_progress = dict(job.progress or {})
                tenant_id, actor_user_id = job.tenant_id, job.created_by_user_id
            core = _PersistentCoreService(
                gateway=self.service.gateway,
                callback=lambda progress: self._persist(job_id, progress),
            )
            core.pending[plan.plan_id] = plan
            core_job = ImageSyncJob(
                job_id=str(job_id),
                plan_id=plan.plan_id,
                state="validating",
                target_name=plan.target.sheet_name,
                target_url=plan.target.url,
                import_batch=plan.import_batch,
                target_key=str(initial_progress.get("targetKey") or ""),
                rows_total=len(plan.rows),
                fill_order_background=initial_progress.get('fillOrderBackground', True),
                background_plan_indices=initial_progress.get('backgroundPlanIndices'),
            )
            core.sync_jobs[str(job_id)] = core_job
            core._run_sheet_sync(str(job_id), plan, plan.target)
            final = core.sync_jobs[str(job_id)].public()
            self._persist(job_id, final)
            if final['state'] == 'completed':
                try:
                    with self.service.session_factory() as session:
                        touch_target(session, tenant_id, actor_user_id, plan.target.url, plan.target.sheet_id)
                        session.commit()
                except Exception:
                    # A preference failure must not turn a completed import
                    # into a failed business task or expose target details.
                    logger.warning('Could not update procurement destination usage time')
        except CloudProcurementImportError as exc:
            self._fail(job_id, exc.code, str(exc))
        except Exception:
            logger.exception("Cloud procurement import worker failed")
            self._fail(
                job_id,
                "procurement_import_worker_failed",
                "云端导入任务发生未预期错误，已停止继续写入",
            )
