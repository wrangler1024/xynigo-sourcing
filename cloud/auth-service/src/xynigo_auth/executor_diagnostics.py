"""Credential-free executor diagnostics, retained independently of task writes."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .business_log import sanitize_log_value
from .models import ExecutorTask, ExecutorTaskEvent, LocalExecutor, LogisticsQueryResult, LogisticsQueryRun
from .system_log import SystemLogService


Serial = Annotated[str, Field(pattern=r"^[0-9]{1,20}$")]
Reason = Annotated[str, Field(max_length=128, pattern=r"^(hubstudio_[a-z_]+)?$")]
ApiCode = Annotated[str, Field(max_length=16, pattern=r"^(-?[0-9]{1,10}|E[0-9]{6})?$")]
Count = Annotated[int, Field(strict=True, ge=0, le=86_400_000)]


class CloseDiagnostic(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    environmentSerial: Serial
    state: Literal["open", "opening", "closing", "closed", "absent", "unknown"]
    confirmed: bool
    stopSent: bool
    stopAttempts: Count
    statusChecks: Count
    elapsedMs: Count
    stopErrorCode: Reason = ""
    stopApiCode: ApiCode = ""
    statusErrorCode: Reason = ""
    statusApiCode: ApiCode = ""
    firstErrorOperation: Literal["", "browser_stop", "browser_status"] = ""
    firstErrorCode: Reason = ""
    firstApiCode: ApiCode = ""


class LogisticsDiagnostics(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    schemaVersion: Literal[1] = 1
    resourceConstrained: bool
    effectiveConcurrency: Annotated[int, Field(ge=1, le=5)]
    pendingCloseCount: Count
    closeChecks: list[CloseDiagnostic] = Field(default_factory=list, max_length=20)


def validate_diagnostics(raw: Any, serials: set[str]) -> dict[str, Any]:
    data = LogisticsDiagnostics.model_validate(raw)
    supplied = [item.environmentSerial for item in data.closeChecks]
    if len(supplied) != len(set(supplied)) or not set(supplied).issubset(serials):
        raise ValueError("diagnostics_environment_scope_invalid")
    return data.model_dump()


def executor_context(session: Session, executor: LocalExecutor, task: ExecutorTask,
                     body: Any) -> dict[str, Any]:
    """Call only after verifying that the authenticated device owns the task.

    Project explicit identifiers/counts; never persist request bodies, lease
    credentials, exception text, screenshot bytes, or vendor messages.
    """
    context: dict[str, Any] = {
        "taskId": str(task.id), "executorId": str(executor.id),
        "taskType": task.task_type, "platform": executor.platform,
        "attempt": task.attempt,
    }
    run = session.scalar(select(LogisticsQueryRun).where(
        LogisticsQueryRun.executor_task_id == task.id,
        LogisticsQueryRun.tenant_id == task.tenant_id,
    ))
    if run is None:
        return context
    context.update(runId=str(run.id), queryId=str(run.root_run_id or run.id),
                   expectedRowCount=run.total_count)
    allowed = set((run.request_summary or {}).get("environmentSerials") or [])
    phase = getattr(body, "phase", None)
    if phase:
        context["phase"] = phase
    snapshot = getattr(body, "snapshot", None)
    if isinstance(snapshot, dict):
        rows = snapshot.get("rows")
        if isinstance(rows, list):
            serials = [r.get("environmentSerial") for r in rows if isinstance(r, dict)
                       and isinstance(r.get("environmentSerial"), str)]
            context.update(
                submittedRowCount=len(rows),
                unexpectedRowCount=sum(s not in allowed for s in serials),
                duplicateRowCount=len(serials) - len(set(serials)),
            )
        if "diagnostics" in snapshot:
            try:
                context["diagnostics"] = validate_diagnostics(snapshot["diagnostics"], allowed)
            except (ValidationError, ValueError):
                context["diagnosticsInvalid"] = True
    result = getattr(body, "resultSummary", None)
    if isinstance(result, dict):
        counts = {key: value for key, value in result.items()
                  if key in {"totalCount", "successCount", "failedCount", "stoppedCount"}
                  and type(value) is int and 0 <= value <= 86_400_000}
        context["reportedCounts"] = counts
        context["resultCountsConsistent"] = (
            counts.get("totalCount") == run.total_count
            and sum(counts.get(key, 0) for key in
                    ("successCount", "failedCount", "stoppedCount")) <= run.total_count
        )
    return context


def logistics_diagnostics(session: Session, run: LogisticsQueryRun, *,
                          page: int, page_size: int) -> dict[str, Any]:
    """Read one attempt. Pagination is explicit for every potentially long list."""
    task = session.get(ExecutorTask, run.executor_task_id) if run.executor_task_id else None
    executor = session.get(LocalExecutor, run.executor_id) if run.executor_id else None
    row_filter = [LogisticsQueryResult.run_id == run.id,
                  LogisticsQueryResult.tenant_id == run.tenant_id]
    anomaly_filter = row_filter + [
        (LogisticsQueryResult.status != "ok")
        | (func.coalesce(LogisticsQueryResult.error_summary, "") != "")]
    anomalies = list(session.scalars(select(LogisticsQueryResult).where(*anomaly_filter)
        .order_by(LogisticsQueryResult.environment_serial, LogisticsQueryResult.id)
        .offset((page - 1) * page_size).limit(page_size)))
    event_filter = [ExecutorTaskEvent.task_id == run.executor_task_id,
                    ExecutorTaskEvent.tenant_id == run.tenant_id]
    events = list(session.scalars(select(ExecutorTaskEvent).where(*event_filter)
        .order_by(ExecutorTaskEvent.created_at, ExecutorTaskEvent.id)
        .offset((page - 1) * page_size).limit(page_size))) if task else []
    logs = SystemLogService(session).list_events(
        tenant_id=run.tenant_id, run_id=run.id, page=page, page_size=page_size,
        include_details=True,
    )
    runtime = (run.request_summary or {}).get("runtimeDiagnostics")
    related_filter = [LogisticsQueryRun.tenant_id == run.tenant_id,
        (LogisticsQueryRun.root_run_id == (run.root_run_id or run.id))
        | (LogisticsQueryRun.id == (run.root_run_id or run.id))]
    related = list(session.scalars(select(LogisticsQueryRun).where(*related_filter)
        .order_by(LogisticsQueryRun.created_at, LogisticsQueryRun.id)
        .offset((page - 1) * page_size).limit(page_size)))
    return {
        "runId": str(run.id), "queryId": str(run.root_run_id or run.id),
        "parentRunId": str(run.parent_run_id) if run.parent_run_id else None,
        "taskId": str(task.id) if task else None,
        "status": run.status, "phase": run.phase, "attempt": run.attempt,
        "startedAt": run.started_at, "completedAt": run.completed_at,
        "lastHeartbeatAt": run.last_heartbeat_at,
        "counts": {"total": run.total_count, "success": run.success_count,
                   "failed": run.failed_count},
        "executor": ({"name": sanitize_log_value(executor.display_name),
                      "platform": executor.platform,
                      "currentVersion": sanitize_log_value(executor.client_version)}
                     if executor else None),
        "runtimeDiagnostics": runtime,
        "runtimeDiagnosticsAt": (run.request_summary or {}).get("runtimeDiagnosticsAt"),
        "diagnosticsAvailable": runtime is not None,
        "diagnosticsNote": (None if runtime is not None else
            "此任务未上传关闭诊断；旧执行器或尚未上报，不能据此判断环境已关闭。"),
        "anomalies": {"page": page, "pageSize": page_size,
            "total": session.scalar(select(func.count()).select_from(LogisticsQueryResult)
                                    .where(*anomaly_filter)),
            "items": [{"environmentSerial": r.environment_serial, "status": r.status,
                       "errorSummary": sanitize_log_value(r.error_summary or ""),
                       "executionDurationMs": r.execution_duration_ms,
                       "executionAttempted": r.execution_attempted} for r in anomalies]},
        "timeline": {"page": page, "pageSize": page_size,
            "total": session.scalar(select(func.count()).select_from(ExecutorTaskEvent)
                                    .where(*event_filter)) if task else 0,
            "items": [{"eventType": e.event_type, "phase": e.phase,
                       "code": e.stable_code, "current": e.progress_current,
                       "total": e.progress_total, "at": e.created_at,
                       "traceId": e.trace_id} for e in events]},
        "httpEvents": logs,
        "relatedRuns": {"page": page, "pageSize": page_size,
            "total": session.scalar(select(func.count()).select_from(LogisticsQueryRun)
                                    .where(*related_filter)),
            "items": [{"runId": str(r.id), "queryMode": r.query_mode,
                       "status": r.status, "createdAt": r.created_at} for r in related]},
    }
