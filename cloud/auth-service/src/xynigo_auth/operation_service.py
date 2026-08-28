"""Durable ingestion of real local procurement-operation results."""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .buyer_account_sync import enqueue_buyer_account_mirror
from .models import (
    BuyerAccount,
    EnvironmentCreationResult,
    EnvironmentCreationRun,
    LogisticsQueryResult,
    LogisticsQueryRun,
    OperationalSyncOutbox,
)
from .operation_contract import EnvironmentCreationRunBody, LogisticsQueryRunBody
from .purchase_service import PurchaseServiceError


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _payload_hash(body: EnvironmentCreationRunBody | LogisticsQueryRunBody) -> str:
    canonical = json.dumps(
        body.model_dump(mode="json"),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _milliseconds(value: datetime | None) -> int | None:
    return int(value.timestamp() * 1000) if value is not None else None


def _nonempty(fields: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in fields.items() if value not in (None, "")}


def _run_status(success_count: int, total_count: int) -> str:
    if success_count == total_count:
        return "completed"
    if success_count:
        return "partial_failure"
    return "failed"


class OperationResultService:
    def __init__(self, session: Session) -> None:
        self.session = session

    def ingest_environment_run(
        self,
        *,
        tenant_id: uuid.UUID,
        actor_user_id: uuid.UUID,
        client_version: str | None,
        body: EnvironmentCreationRunBody,
    ) -> dict[str, object]:
        digest = _payload_hash(body)
        existing = self.session.scalar(
            select(EnvironmentCreationRun).where(
                EnvironmentCreationRun.tenant_id == tenant_id,
                EnvironmentCreationRun.source_run_key == body.runKey,
            )
        )
        if existing is not None:
            if existing.payload_hash != digest:
                raise PurchaseServiceError(
                    "operation_run_idempotency_conflict",
                    "同一建环境任务标识已提交不同结果",
                    409,
                )
            return self._environment_result(existing, unchanged=True)

        now = utcnow()
        success_count = sum(item.status == "success" for item in body.results)
        run = EnvironmentCreationRun(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            actor_user_id=actor_user_id,
            source_run_key=body.runKey,
            payload_hash=digest,
            site=body.site,
            purchase_date=body.purchaseDate,
            environment_group=body.environmentGroup,
            status=_run_status(success_count, len(body.results)),
            total_count=len(body.results),
            success_count=success_count,
            failed_count=len(body.results) - success_count,
            ip_ok_count=sum(item.ok for item in body.ipChecks),
            ip_total_count=len(body.ipChecks),
            started_at=body.startedAt,
            completed_at=body.completedAt,
            source=body.source,
            client_version=client_version,
            created_at=now,
        )
        self.session.add(run)
        # No ORM relationship links these rows, so SQLAlchemy cannot infer
        # that the parent must be inserted before the result batch.
        self.session.flush()
        ip_checks = {item.environmentName: item for item in body.ipChecks}
        resource_conflicts = 0
        for item in body.results:
            ip_check = ip_checks.get(item.environmentName)
            record = EnvironmentCreationResult(
                id=uuid.uuid4(),
                run_id=run.id,
                tenant_id=tenant_id,
                account_ref=item.accountRef,
                account_label=item.accountLabel,
                purchaser_label=item.purchaserLabel,
                environment_name=item.environmentName,
                environment_ref=item.environmentRef,
                environment_serial=item.environmentSerial,
                status=item.status,
                error_step=item.errorStep or None,
                error_summary=item.errorSummary or None,
                binding_at=item.bindingAt,
                recovered_existing=item.recoveredExisting,
                ip_address=ip_check.ipAddress if ip_check else None,
                ip_country=ip_check.country if ip_check else None,
                ip_city=ip_check.city if ip_check else None,
                ip_isp=ip_check.isp if ip_check else None,
                ip_verified=ip_check.ok if ip_check else None,
                feishu_sync_status="pending",
                created_at=now,
            )
            self.session.add(record)
            fields = _nonempty({
                "同步键": f"environment:{tenant_id}:{record.id}",
                "建环境任务ID": body.runKey,
                "站点": body.site,
                "购买日期": body.purchaseDate,
                "环境分组": body.environmentGroup,
                "采购员": item.purchaserLabel,
                "买家号引用": item.accountRef,
                "买家号脱敏标签": item.accountLabel,
                "Hub环境名称": item.environmentName,
                "Hub环境编号": item.environmentRef,
                "环境序号": item.environmentSerial,
                "执行状态": "成功" if item.status == "success" else "失败",
                "失败步骤": item.errorStep,
                "错误摘要": item.errorSummary,
                "绑定时间": _milliseconds(item.bindingAt),
                "已有环境恢复": item.recoveredExisting,
                "出口IP": ip_check.ipAddress if ip_check else "",
                "IP国家": ip_check.country if ip_check else "",
                "IP城市": ip_check.city if ip_check else "",
                "IP运营商": ip_check.isp if ip_check else "",
                "IP验证结果": (
                    "通过" if ip_check and ip_check.ok
                    else "未通过" if ip_check
                    else "未检测"
                ),
                "服务端记录ID": str(record.id),
                "数据库写入时间": _milliseconds(now),
                "回传来源": body.source,
                "客户端版本": client_version or "",
            })
            self.session.add(
                OperationalSyncOutbox(
                    id=uuid.uuid4(),
                    tenant_id=tenant_id,
                    aggregate_type="environment_creation_result",
                    aggregate_id=record.id,
                    dedupe_key=f"environment_creation_result:{record.id}",
                    payload={"syncKey": fields["同步键"], "fields": fields},
                    status="pending",
                    attempt_count=0,
                    available_at=now,
                )
            )
            if item.status == "success":
                resource_conflicts += self._merge_buyer_resource(
                    tenant_id=tenant_id,
                    run_key=body.runKey,
                    site=body.site,
                    item=item,
                    now=now,
                )
        self.session.flush()
        result = self._environment_result(run, unchanged=False)
        result["resourceConflictCount"] = resource_conflicts
        return result

    def _merge_buyer_resource(
        self,
        *,
        tenant_id: uuid.UUID,
        run_key: str,
        site: str,
        item: Any,
        now: datetime,
    ) -> int:
        account = self.session.scalar(
            select(BuyerAccount).where(
                BuyerAccount.tenant_id == tenant_id,
                BuyerAccount.account_ref == item.accountRef,
            )
        )
        if account is None:
            account = BuyerAccount(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                account_ref=item.accountRef,
                display_label=item.accountLabel,
                site=site,
                status="available",
                source_availability_status="available",
                credential_status="ready",
                source="environment_creation",
                source_status="已绑定环境",
                hub_environment_ref=item.environmentRef,
                hub_environment_name=item.environmentName,
                operator_label=item.purchaserLabel,
                last_snapshot_key=run_key,
                source_updated_at=item.bindingAt,
                last_synced_at=now,
                feishu_sync_status="pending",
                version=1,
                created_at=now,
                updated_at=now,
            )
            self.session.add(account)
            enqueue_buyer_account_mirror(self.session, account, available_at=now)
            return 0
        if account.site != site:
            return 1
        if account.hub_environment_ref and account.hub_environment_ref != item.environmentRef:
            return 1
        account.display_label = item.accountLabel
        account.credential_status = "ready"
        account.source = "environment_creation"
        account.source_status = "已绑定环境"
        account.hub_environment_ref = item.environmentRef
        account.hub_environment_name = item.environmentName
        account.operator_label = item.purchaserLabel
        account.last_snapshot_key = run_key
        account.source_updated_at = item.bindingAt
        account.last_synced_at = now
        account.version += 1
        account.updated_at = now
        enqueue_buyer_account_mirror(self.session, account, available_at=now)
        return 0

    def ingest_logistics_run(
        self,
        *,
        tenant_id: uuid.UUID,
        actor_user_id: uuid.UUID,
        client_version: str | None,
        body: LogisticsQueryRunBody,
    ) -> dict[str, object]:
        digest = _payload_hash(body)
        existing = self.session.scalar(
            select(LogisticsQueryRun).where(
                LogisticsQueryRun.tenant_id == tenant_id,
                LogisticsQueryRun.source_run_key == body.runKey,
            )
        )
        if existing is not None:
            if existing.payload_hash != digest:
                raise PurchaseServiceError(
                    "operation_run_idempotency_conflict",
                    "同一物流查询任务标识已提交不同结果",
                    409,
                )
            return self._logistics_result(existing, unchanged=True)

        now = utcnow()
        success_count = sum(item.status == "ok" for item in body.results)
        run = LogisticsQueryRun(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            actor_user_id=actor_user_id,
            source_run_key=body.runKey,
            payload_hash=digest,
            query_mode=body.queryMode,
            site=body.site,
            status=_run_status(success_count, len(body.results)),
            total_count=len(body.results),
            success_count=success_count,
            failed_count=len(body.results) - success_count,
            started_at=body.startedAt,
            completed_at=body.completedAt,
            source=body.source,
            client_version=client_version,
            created_at=now,
        )
        self.session.add(run)
        self.session.flush()
        mode_label = {
            "initial": "首次查询",
            "single_retry": "单条重查",
            "failed_retry": "异常重查",
        }[body.queryMode]
        status_labels = {
            "ok": "成功",
            "fail": "失败",
            "login": "需登录",
            "inuse": "使用中",
            "stopped": "已停止",
            "pending": "待处理",
        }
        for item in body.results:
            record = LogisticsQueryResult(
                id=uuid.uuid4(),
                run_id=run.id,
                tenant_id=tenant_id,
                environment_serial=item.environmentSerial,
                environment_name=item.environmentName or None,
                status=item.status,
                platform_order_no=item.platformOrderNo or None,
                order_time_text=item.orderTime or None,
                amount_text=item.amount or None,
                platform_status=item.platformStatus or None,
                status_label=item.statusLabel or None,
                fulfillment_stage=item.fulfillmentStage or None,
                tracking_numbers=list(item.trackingNumbers),
                package_numbers=list(item.packageNumbers),
                carrier=item.carrier or None,
                cancelled=item.cancelled,
                risk_order=item.riskOrder,
                risk_summary=item.riskSummary or None,
                ip_address=item.ipAddress or None,
                time_zone=item.timeZone or None,
                utc_offset_minutes=item.utcOffsetMinutes,
                queried_at=item.queriedAt,
                error_summary=item.errorSummary or None,
                screenshot_status=item.screenshotStatus or None,
                feishu_sync_status="pending",
                created_at=now,
            )
            self.session.add(record)
            fields = _nonempty({
                "同步键": f"logistics:{tenant_id}:{record.id}",
                "查询任务ID": body.runKey,
                "查询模式": mode_label,
                "站点": body.site,
                "环境序号": item.environmentSerial,
                "环境名称": item.environmentName,
                "查询状态": status_labels[item.status],
                "平台订单号": item.platformOrderNo,
                "下单时间": item.orderTime,
                "订单金额": item.amount,
                "平台状态": item.platformStatus,
                "中文状态": item.statusLabel,
                "履约阶段": item.fulfillmentStage,
                "物流单号": "\n".join(item.trackingNumbers),
                "包裹号": "\n".join(item.packageNumbers),
                "承运商": item.carrier,
                "砍单": item.cancelled,
                "风险订单": item.riskOrder,
                "风险提示": item.riskSummary,
                "出口IP": item.ipAddress,
                "时区": item.timeZone,
                "UTC偏移分钟": item.utcOffsetMinutes,
                "查询时间": _milliseconds(item.queriedAt),
                "错误摘要": item.errorSummary,
                "轨迹截图状态": item.screenshotStatus,
                "服务端记录ID": str(record.id),
                "数据库写入时间": _milliseconds(now),
                "回传来源": body.source,
                "客户端版本": client_version or "",
            })
            self.session.add(
                OperationalSyncOutbox(
                    id=uuid.uuid4(),
                    tenant_id=tenant_id,
                    aggregate_type="logistics_query_result",
                    aggregate_id=record.id,
                    dedupe_key=f"logistics_query_result:{record.id}",
                    payload={"syncKey": fields["同步键"], "fields": fields},
                    status="pending",
                    attempt_count=0,
                    available_at=now,
                )
            )
        self.session.flush()
        return self._logistics_result(run, unchanged=False)

    def _environment_result(
        self, run: EnvironmentCreationRun, *, unchanged: bool
    ) -> dict[str, object]:
        return {
            "runId": str(run.id),
            "runKey": run.source_run_key,
            "status": run.status,
            "totalCount": run.total_count,
            "successCount": run.success_count,
            "failedCount": run.failed_count,
            "ipOkCount": run.ip_ok_count,
            "ipTotalCount": run.ip_total_count,
            "syncStatus": "pending",
            "unchanged": unchanged,
        }

    def _logistics_result(
        self, run: LogisticsQueryRun, *, unchanged: bool
    ) -> dict[str, object]:
        return {
            "runId": str(run.id),
            "runKey": run.source_run_key,
            "status": run.status,
            "totalCount": run.total_count,
            "successCount": run.success_count,
            "failedCount": run.failed_count,
            "syncStatus": "pending",
            "unchanged": unchanged,
        }

    def pending_sync_count(self, *, tenant_id: uuid.UUID) -> int:
        return int(
            self.session.scalar(
                select(func.count())
                .select_from(OperationalSyncOutbox)
                .where(
                    OperationalSyncOutbox.tenant_id == tenant_id,
                    OperationalSyncOutbox.status.in_(("pending", "processing")),
                )
            )
            or 0
        )
