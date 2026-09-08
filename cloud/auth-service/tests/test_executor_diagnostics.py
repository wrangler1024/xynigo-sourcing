from __future__ import annotations

import json
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from test_auth_flow import build_test_app
from test_business_logs import add_member_session
from test_executor_channel import CSRF, create_pairing_code, device_headers, heartbeat, login, pair
from xynigo_auth.models import LogisticsQueryRun, SystemLogEvent, Tenant


@pytest.fixture
def task_setup(tmp_path):
    app, database, _ = build_test_app(tmp_path)
    web, device = TestClient(app), TestClient(app)
    login(web)
    capabilities = ["logistics.query.v1", "workspace.rpc.v1"]
    paired = pair(device, create_pairing_code(web), capabilities=capabilities)
    credential = str(paired["deviceCredential"])
    heartbeat(device, credential, capabilities=capabilities, client_version="0.17.8")
    response = web.post("/v1/operation-runs/logistics-query", json={
        "idempotencyKey": "synthetic-diagnostics-task-001",
        "executorId": paired["executorId"], "queryMode": "initial",
        "site": "MX", "environmentSerials": ["1001"],
    }, headers=CSRF)
    assert response.status_code == 202, response.text
    run = response.json()["data"]
    leased = heartbeat(device, credential, capabilities=capabilities,
                       client_version="0.17.8")["task"]
    assert leased["payload"]["diagnosticsVersion"] == 1
    headers = device_headers(credential)
    prefix = f"/v1/executor-channel/tasks/{leased['id']}"
    assert device.post(prefix + "/start", json={"leaseToken": leased["leaseToken"]},
                       headers=headers).status_code == 200
    body = {"leaseToken": leased["leaseToken"], "phase": "logistics.waiting_cleanup",
            "current": 1, "total": 1, "snapshot": {"rows": [row()]}}
    yield web, device, database, run, prefix, headers, body
    web.close()
    device.close()


def row(serial="1001"):
    return {"environmentSerial": serial, "status": "ok", "completedSteps": ["query_completed"],
            "errorSummary": "查询已完成，但该环境关闭状态未确认", "executionDurationMs": 62000}


def diagnostics():
    return {"schemaVersion": 1, "resourceConstrained": True,
            "effectiveConcurrency": 1, "pendingCloseCount": 1,
            "closeChecks": [{"environmentSerial": "1001", "state": "closing",
                "confirmed": False, "stopSent": True, "stopAttempts": 1,
                "statusChecks": 4, "elapsedMs": 60000,
                "stopErrorCode": "hubstudio_local_api_timeout", "stopApiCode": "",
                "statusErrorCode": "hubstudio_local_api_rate_limited", "statusApiCode": "E010205",
                "firstErrorOperation": "browser_stop",
                "firstErrorCode": "hubstudio_local_api_timeout", "firstApiCode": ""}]}


def test_rejected_progress_is_correlated_and_persists_after_rollback(task_setup):
    web, device, db, run, prefix, headers, body = task_setup
    body["snapshot"]["rows"] += [{**row("1002"), "platformOrderNo": "PRIVATE-ORDER-SENTINEL",
                                  "errorSummary": "password=PRIVATE-SECRET-SENTINEL"}]
    response = device.post(prefix + "/progress", json=body, headers=headers)
    assert response.status_code == 422
    with db.session_factory() as session:
        record = session.scalar(select(SystemLogEvent).where(
            SystemLogEvent.request_id == response.headers["X-Request-ID"]))
        assert record.error_code == "executor_progress_snapshot_invalid"
        assert record.client_version == "0.17.8"  # verified device, not stale request header
        assert record.details["runId"] == run["runId"]
        assert record.details["submittedRowCount"] == 2
        assert record.details["expectedRowCount"] == 1
        assert record.details["rejectionReason"] == "row_count_exceeds_task"
        assert "SENTINEL" not in json.dumps(record.details)
        stored_run = session.get(LogisticsQueryRun, uuid.UUID(run["runId"]))
        assert stored_run.phase != "logistics.waiting_cleanup"
    db.engine.dispose()
    listing = web.get("/v1/system-logs", params={"runId": run["runId"], "statusCode": 422})
    assert listing.json()["data"]["total"] == 1
    by_keyword = web.get("/v1/system-logs", params={"keyword": run["runId"], "statusCode": 422})
    assert by_keyword.json()["data"]["total"] == 1
    by_task = web.get("/v1/system-logs", params={"taskId": run["executorTaskId"], "statusCode": 422})
    assert by_task.json()["data"]["total"] == 1
    diagnostic = web.get(f"/v1/operation-runs/logistics-query/{run['runId']}/diagnostics").json()["data"]
    assert diagnostic["diagnosticsAvailable"] is False
    assert diagnostic["httpEvents"]["items"][0]["details"]["rejectionReason"] == "row_count_exceeds_task"


def test_close_diagnostics_persist_and_are_tenant_scoped(task_setup):
    web, device, db, run, prefix, headers, body = task_setup
    body["snapshot"]["diagnostics"] = diagnostics()
    response = device.post(prefix + "/progress", json=body, headers=headers)
    assert response.status_code == 200, response.text
    url = f"/v1/operation-runs/logistics-query/{run['runId']}/diagnostics"
    data = web.get(url).json()["data"]
    assert data["runtimeDiagnostics"]["closeChecks"][0]["statusApiCode"] == "E010205"
    assert data["runtimeDiagnostics"]["closeChecks"][0]["firstErrorOperation"] == "browser_stop"
    assert data["diagnosticsAvailable"] is True
    assert data["anomalies"]["items"][0]["status"] == "ok"  # successful row warning is visible
    assert data["anomalies"]["total"] == 1
    assert data["runtimeDiagnosticsAt"]
    assert data["timeline"]["items"][-1]["phase"] == "logistics.waiting_cleanup"
    assert "leaseToken" not in json.dumps(data)
    assert "platformOrderNo" not in json.dumps(data)
    anonymous = device.get(url)
    assert anonymous.status_code == 401
    _, member_headers = add_member_session(db)
    assert device.get(url, headers=member_headers).status_code == 403
    with db.session_factory() as session:
        tenant = Tenant(feishu_tenant_key="other-tenant-diagnostics", name="Other synthetic tenant")
        session.add(tenant)
        session.flush()
        stored = session.get(LogisticsQueryRun, uuid.UUID(run["runId"]))
        stored.tenant_id = tenant.id
        session.commit()
    assert web.get(url).status_code == 404


@pytest.mark.parametrize("mutation", ["extra_field", "wrong_environment", "unsafe_code", "wrong_row"])
def test_rejects_unsafe_diagnostic_or_environment_payload(task_setup, mutation):
    web, device, db, run, prefix, headers, body = task_setup
    body["snapshot"]["diagnostics"] = diagnostics()
    check = body["snapshot"]["diagnostics"]["closeChecks"][0]
    if mutation == "extra_field":
        check["rawMessage"] = "SENSITIVE-DIAGNOSTIC-SENTINEL"
    elif mutation == "wrong_environment":
        check["environmentSerial"] = "9999"
    elif mutation == "unsafe_code":
        check["stopApiCode"] = "secret=https://SENSITIVE-DIAGNOSTIC-SENTINEL"
    else:
        body["snapshot"]["rows"][0]["environmentSerial"] = "9999"
    response = device.post(prefix + "/progress", json=body, headers=headers)
    assert response.status_code == 422
    with db.session_factory() as session:
        log = session.scalar(select(SystemLogEvent).where(
            SystemLogEvent.request_id == response.headers["X-Request-ID"]))
        assert "SENSITIVE-DIAGNOSTIC-SENTINEL" not in json.dumps(log.details)
        assert log.details["rejectionReason"] in {
            "diagnostics_schema_or_scope_invalid", "environment_outside_task"}
        assert "runtimeDiagnostics" not in session.get(
            LogisticsQueryRun, uuid.UUID(run["runId"])).request_summary


def test_invalid_request_body_and_wrong_device_do_not_leak_task_context(task_setup):
    web, device, db, run, prefix, headers, body = task_setup
    invalid = {**body, "current": "credential=SENTINEL"}
    response = device.post(prefix + "/progress", json=invalid, headers=headers)
    assert response.status_code == 422
    with db.session_factory() as session:
        log = session.scalar(select(SystemLogEvent).where(
            SystemLogEvent.request_id == response.headers["X-Request-ID"]))
        assert log.details["runId"] == run["runId"]
        assert log.details["rejectionReason"] == "request_schema_invalid"
        assert "SENTINEL" not in json.dumps(log.details)
    second = pair(device, create_pairing_code(web))
    response = device.post(prefix + "/progress", json=body,
                           headers=device_headers(str(second["deviceCredential"])))
    assert response.status_code in (403, 404)
    with db.session_factory() as session:
        log = session.scalar(select(SystemLogEvent).where(
            SystemLogEvent.request_id == response.headers["X-Request-ID"]))
        assert "taskId" not in log.details and "runId" not in log.details


def test_old_executor_finish_mismatch_is_visible_and_pagination_is_bounded(task_setup):
    web, device, db, run, prefix, headers, body = task_setup
    assert device.post(prefix + "/progress", json=body, headers=headers).status_code == 200
    response = device.post(prefix + "/finish", json={
        "leaseToken": body["leaseToken"], "outcome": "succeeded",
        "resultCode": "logistics_partial_failure", "resultSummary": {
            "runStatus": "partial_failure", "phase": "logistics.partial_failure",
            "totalCount": 1, "successCount": 12, "failedCount": 11, "stoppedCount": 0,
            "progressCompleted": 1, "progressTotal": 1, "errorCode": "", "errorSummary": "",
        }}, headers=headers)
    assert response.status_code == 200, response.text  # backwards-compatible terminal acceptance
    with db.session_factory() as session:
        log = session.scalar(select(SystemLogEvent).where(
            SystemLogEvent.request_id == response.headers["X-Request-ID"]))
        assert log.level == "warning" and log.error_code == "executor_result_count_mismatch"
    url = f"/v1/operation-runs/logistics-query/{run['runId']}/diagnostics"
    one = web.get(url, params={"pageSize": 1}).json()["data"]
    two = web.get(url, params={"pageSize": 1, "page": 2}).json()["data"]
    assert one["httpEvents"]["total"] >= 3
    assert len(one["httpEvents"]["items"]) == 1
    assert one["timeline"]["items"] != two["timeline"]["items"]
    assert one["httpEvents"]["items"] != two["httpEvents"]["items"]
    assert one["diagnosticsAvailable"] is False
