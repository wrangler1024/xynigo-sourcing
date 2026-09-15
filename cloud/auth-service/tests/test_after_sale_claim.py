# -*- coding: utf-8 -*-
"""售后处理（after.sale.scan.v1 / after.sale.claim.v1）云端全链契约测试。

覆盖：扫描建任务→进度→终态 GET（summary.rows / claimableCount）；
提交建 Run（幂等）→进度行+截图附件→终态快照（行数/终态/截图可读）；
扫描与提交的取消路由把 stop 请求下发到任务；闭集与跨批次白名单 422。
"""
from __future__ import annotations

import base64
import hashlib
import uuid

from fastapi.testclient import TestClient
from sqlalchemy import select

from test_executor_channel import (
    CSRF,
    create_pairing_code,
    device_headers,
    heartbeat,
    login,
    pair,
)
from test_purchase_api import build_test_app

AS_CAPABILITIES = [
    "config.read.v1",
    "config.write.v1",
    "workspace.snapshot.v1",
    "after.sale.scan.v1",
    "after.sale.claim.v1",
]
CLIENT_VERSION = "0.17.20"
JPEG = b"\xff\xd8synthetic-after-sale-screenshot\xff\xd9"


def _e2e_setup(tmp_path):
    app, database, _oauth = build_test_app(tmp_path)
    with TestClient(app) as web_client, TestClient(app) as device_client:
        login(web_client)
        paired = pair(
            device_client,
            create_pairing_code(web_client),
            capabilities=AS_CAPABILITIES,
        )
        yield web_client, device_client, {
            "executorId": str(paired["executorId"]),
            "credential": str(paired["deviceCredential"]),
        }, database


def _scan_body(*serials: str) -> dict[str, object]:
    return {
        "idempotencyKey": "as-scan-e2e-00000001",
        "executorId": "",
        "browserMode": "visible",
        "environmentSerials": list(serials or ("4902", "4901")),
    }


def _scan_row(serial: str, *, order_no: str, claimable: bool) -> dict[str, object]:
    """执行器投影后的扫描行（闭集，无 screenshotStatus 等本地字段）。"""
    return {
        "environmentSerial": serial,
        "storeName": "合成店铺-" + serial,
        "accountName": "buyer@example.test",
        "orderNo": order_no,
        "deliveredAt": "2026-09-10",
        "amount": "MXN289.00",
        "status": "ok",
        "claimable": claimable,
        "packageCount": 1 if claimable else 0,
        "trackingNo": "SHEIN" + serial,
        "errorSummary": None,
        "screenshotSha256": None,
    }


def _claim_row(order_no: str, serial: str, *, status: str = "ok") -> dict[str, object]:
    """执行器投影后的提交行（闭集 + 本地会带的 packageCount）。"""
    return {
        "orderNo": order_no,
        "environmentSerial": serial,
        "storeName": "合成店铺-" + serial,
        "status": status,
        "packageNo": "PKG" + order_no[-4:],
        "refundBillId": "RB" + order_no[-4:],
        "refundPath": "原路退回（Cuenta original de pago）",
        "durationSeconds": 42,
        "submittedAt": "2026-09-15T03:00:00+00:00",
        "note": "",
        "errorSummary": None,
        "screenshotSha256": None,
        "packageCount": 1,
    }


def _lease_and_start(device_client, credential, *, expect_type: str) -> tuple[str, str]:
    lease = heartbeat(device_client, credential, capabilities=AS_CAPABILITIES,
                      client_version=CLIENT_VERSION)["task"]
    assert lease is not None and lease["type"] == expect_type, lease
    task_id, lease_token = lease["id"], lease["leaseToken"]
    started = device_client.post(
        f"/v1/executor-channel/tasks/{task_id}/start",
        json={"leaseToken": lease_token},
        headers=device_headers(credential),
    )
    assert started.status_code == 200, started.text
    return task_id, lease_token


# ===== 扫描：建任务 → 进度 → 终态 GET =====
def test_after_sale_scan_progress_and_terminal_summary(tmp_path) -> None:
    for web_client, device_client, ids, database in _e2e_setup(tmp_path):
        executor_id, credential = ids["executorId"], ids["credential"]
        heartbeat(device_client, credential, capabilities=AS_CAPABILITIES,
                  client_version=CLIENT_VERSION)

        created = web_client.post(
            "/v1/after-sale/scan",
            json={**_scan_body("4902", "4901"), "executorId": executor_id},
            headers=CSRF,
        )
        assert created.status_code == 202, created.text
        payload = created.json()["data"]
        assert set(payload) == {"taskId", "status", "executorId"}
        assert payload["status"] == "queued"
        assert payload["executorId"] == executor_id
        task_id = payload["taskId"]

        # 任务表里没有 Run：扫描只是任务 + 任务行上的进度快照
        with database.session_factory() as session:
            from xynigo_auth.models import AfterSaleClaimRun
            assert session.scalar(select(AfterSaleClaimRun)) is None

        # 运行中 GET：summary 带上计划环境数（进度条分母）
        running = web_client.get(f"/v1/after-sale/scan/{task_id}")
        assert running.status_code == 200, running.text
        data = running.json()["data"]
        assert data["taskId"] == task_id
        assert data["status"] == "queued"
        assert data["summary"] == {"rows": [], "totalCount": 2,
                                   "claimableCount": 0}
        assert data["lastRuns"] == {}

        lease_task, lease_token = _lease_and_start(
            device_client, credential, expect_type="after.sale.scan.v1")
        assert lease_task == task_id

        # 进度：一条可申请、一条无订单（empty），部分行上报
        progress = device_client.post(
            f"/v1/executor-channel/tasks/{task_id}/progress",
            json={
                "leaseToken": lease_token,
                "phase": "after_sale.scan.running",
                "current": 1,
                "total": 2,
                "snapshot": {"rows": [
                    _scan_row("4902", order_no="GSH0001", claimable=True),
                    {
                        "environmentSerial": "4901",
                        "storeName": "合成店铺-4901",
                        "accountName": "",
                        "orderNo": "",
                        "deliveredAt": "",
                        "amount": "",
                        "status": "empty",
                        "claimable": False,
                        "packageCount": 0,
                        "trackingNo": "",
                        "errorSummary": "该环境订单列表没有可申请售后的已送达订单",
                        "screenshotSha256": None,
                    },
                ]},
            },
            headers=device_headers(credential),
        )
        assert progress.status_code == 200, progress.text

        mid = web_client.get(f"/v1/after-sale/scan/{task_id}").json()["data"]
        assert mid["status"] == "running"
        assert mid["summary"]["totalCount"] == 2
        assert len(mid["summary"]["rows"]) == 2
        assert mid["summary"]["claimableCount"] == 1

        finish = device_client.post(
            f"/v1/executor-channel/tasks/{task_id}/finish",
            json={
                "leaseToken": lease_token,
                "outcome": "succeeded",
                "resultCode": "after_sale_scan_completed",
                "resultSummary": {
                    "rows": [
                        _scan_row("4902", order_no="GSH0001", claimable=True),
                        _scan_row("4901", order_no="GSH0002", claimable=False),
                    ],
                    "totalCount": 2,
                    "claimableCount": 1,
                },
            },
            headers=device_headers(credential),
        )
        assert finish.status_code == 200, finish.text

        final = web_client.get(f"/v1/after-sale/scan/{task_id}")
        assert final.status_code == 200, final.text
        summary = final.json()["data"]["summary"]
        assert final.json()["data"]["status"] == "succeeded"
        assert summary["totalCount"] == 2
        assert summary["claimableCount"] == 1
        assert {row["orderNo"] for row in summary["rows"]} == {"GSH0001", "GSH0002"}
        assert summary["rows"][0]["claimable"] is True
        # 闭集：本地字段不进云端快照
        assert "screenshotStatus" not in summary["rows"][0]
        break


def test_after_sale_scan_rejects_row_extra_field_and_cross_batch_serial(tmp_path) -> None:
    """闭集 extra=forbid 与请求白名单：投影外字段/串批次环境序号必须 422。"""
    for web_client, device_client, ids, _database in _e2e_setup(tmp_path):
        executor_id, credential = ids["executorId"], ids["credential"]
        heartbeat(device_client, credential, capabilities=AS_CAPABILITIES,
                  client_version=CLIENT_VERSION)
        created = web_client.post(
            "/v1/after-sale/scan",
            json={**_scan_body("4902"), "executorId": executor_id},
            headers=CSRF,
        )
        assert created.status_code == 202, created.text
        task_id = created.json()["data"]["taskId"]
        _, lease_token = _lease_and_start(
            device_client, credential, expect_type="after.sale.scan.v1")

        with_extra = device_client.post(
            f"/v1/executor-channel/tasks/{task_id}/progress",
            json={
                "leaseToken": lease_token,
                "phase": "after_sale.scan.running",
                "current": 1,
                "total": 1,
                "snapshot": {"rows": [
                    {**_scan_row("4902", order_no="GSH0001", claimable=True),
                     "screenshotStatus": "ok"},
                ]},
            },
            headers=device_headers(credential),
        )
        assert with_extra.status_code == 422, with_extra.text

        outside = device_client.post(
            f"/v1/executor-channel/tasks/{task_id}/progress",
            json={
                "leaseToken": lease_token,
                "phase": "after_sale.scan.running",
                "current": 1,
                "total": 1,
                "snapshot": {"rows": [
                    _scan_row("9999", order_no="GSH0001", claimable=True),
                ]},
            },
            headers=device_headers(credential),
        )
        assert outside.status_code == 422, outside.text
        break


# ===== 提交：建 Run（幂等）→ 进度 + 截图 → 终态快照 =====
def test_after_sale_claim_run_progress_finish_and_snapshot(tmp_path) -> None:
    for web_client, device_client, ids, database in _e2e_setup(tmp_path):
        executor_id, credential = ids["executorId"], ids["credential"]
        heartbeat(device_client, credential, capabilities=AS_CAPABILITIES,
                  client_version=CLIENT_VERSION)
        items = [
            {"environmentSerial": "4902", "orderNo": "GSH0001",
             "storeName": "合成店铺-4902", "packageNo": ""},
            {"environmentSerial": "4901", "orderNo": "GSH0002",
             "storeName": "合成店铺-4901", "packageNo": ""},
        ]
        body = {
            "idempotencyKey": "as-claim-e2e-00000001",
            "executorId": executor_id,
            "browserMode": "visible",
            "items": items,
        }
        created = web_client.post(
            "/v1/operation-runs/after-sale-claim", json=body, headers=CSRF)
        assert created.status_code == 202, created.text
        snapshot = created.json()["data"]
        run_id = snapshot["runId"]
        assert snapshot["status"] == "queued"
        assert snapshot["totalCount"] == 2
        assert snapshot["progressTotal"] == 2
        assert snapshot["progressCompleted"] == 0
        assert snapshot["rows"] == []

        # 幂等键重复：同一 Run，不重复下发任务
        replay = web_client.post(
            "/v1/operation-runs/after-sale-claim", json=body, headers=CSRF)
        assert replay.status_code == 202, replay.text
        assert replay.json()["data"]["runId"] == run_id

        task_id, lease_token = _lease_and_start(
            device_client, credential, expect_type="after.sale.claim.v1")

        sha = hashlib.sha256(JPEG).hexdigest()
        progress = device_client.post(
            f"/v1/executor-channel/tasks/{task_id}/progress",
            json={
                "leaseToken": lease_token,
                "phase": "after_sale.claim.running",
                "current": 1,
                "total": 2,
                "snapshot": {
                    "rows": [
                        _claim_row("GSH0001", "4902"),
                        {**_claim_row("GSH0002", "4901", status="running"),
                         "packageNo": "", "refundBillId": "", "refundPath": "",
                         "durationSeconds": None, "submittedAt": None,
                         "packageCount": None},
                    ],
                    "screenshots": [{
                        "orderNo": "GSH0001",
                        "contentType": "image/jpeg",
                        "contentBase64": base64.b64encode(JPEG).decode(),
                        "sha256": sha,
                        "size": len(JPEG),
                    }],
                },
            },
            headers=device_headers(credential),
        )
        assert progress.status_code == 200, progress.text

        partial = web_client.get(
            f"/v1/operation-runs/after-sale-claim/{run_id}").json()["data"]
        assert partial["status"] == "running"
        assert len(partial["rows"]) == 2
        first = [r for r in partial["rows"] if r["orderNo"] == "GSH0001"][0]
        assert first["status"] == "ok"
        assert first["submittedAt"].startswith("2026-09-15T03:00:00")
        assert first["screenshotSha256"] == sha

        # 收尾 progress：执行器在批次结束后会再报一次全终态行（终态回执不带行）
        final_progress = device_client.post(
            f"/v1/executor-channel/tasks/{task_id}/progress",
            json={
                "leaseToken": lease_token,
                "phase": "after_sale.claim.completed",
                "current": 2,
                "total": 2,
                "snapshot": {"rows": [
                    _claim_row("GSH0001", "4902"),
                    _claim_row("GSH0002", "4901"),
                ]},
            },
            headers=device_headers(credential),
        )
        assert final_progress.status_code == 200, final_progress.text

        finish = device_client.post(
            f"/v1/executor-channel/tasks/{task_id}/finish",
            json={
                "leaseToken": lease_token,
                "outcome": "succeeded",
                "resultCode": "after_sale_completed",
                "resultSummary": {
                    "runStatus": "completed",
                    "phase": "after_sale.completed",
                    "progressCompleted": 2,
                    "progressTotal": 2,
                    "totalCount": 2,
                    "successCount": 2,
                    "failedCount": 0,
                    "skippedCount": 0,
                    "stoppedCount": 0,
                    "errorCode": "",
                    "errorSummary": "",
                },
            },
            headers=device_headers(credential),
        )
        assert finish.status_code == 200, finish.text

        final = web_client.get(
            f"/v1/operation-runs/after-sale-claim/{run_id}").json()["data"]
        assert final["status"] == "completed"
        assert final["phase"] == "after_sale.completed"
        assert final["successCount"] == 2
        assert final["failedCount"] == 0
        assert final["skippedCount"] == 0
        assert final["stoppedCount"] == 0
        assert final["progressCompleted"] == 2
        assert final["progressTotal"] == 2
        assert final["stopRequested"] is False
        assert len(final["rows"]) == 2
        assert {row["status"] for row in final["rows"]} == {"ok"}
        assert final["rows"][0]["orderNo"] == "GSH0001"

        # latest 返回同一批次
        latest = web_client.get("/v1/operation-runs/after-sale-claim/latest")
        assert latest.status_code == 200, latest.text
        assert latest.json()["data"]["runId"] == run_id

        # 截图二进制已随进度落库，且带 7 天过期时间
        with database.session_factory() as session:
            from xynigo_auth.models import AfterSaleClaimResult
            stored = session.scalar(
                select(AfterSaleClaimResult).where(
                    AfterSaleClaimResult.run_id == uuid.UUID(run_id),
                    AfterSaleClaimResult.order_no == "GSH0001",
                )
            )
            assert stored is not None
            assert stored.screenshot_content == JPEG
            assert stored.screenshot_sha256 == sha
            assert stored.screenshot_expires_at is not None
            assert stored.environment_serial == "4902"
            other = session.scalar(
                select(AfterSaleClaimResult).where(
                    AfterSaleClaimResult.run_id == uuid.UUID(run_id),
                    AfterSaleClaimResult.order_no == "GSH0002",
                )
            )
            assert other is not None and other.screenshot_content is None
        break


def test_after_sale_claim_progress_rejects_order_outside_run(tmp_path) -> None:
    """跨批次白名单：orderNo 不在建 Run 清单内必须 422（不落脏行）。"""
    for web_client, device_client, ids, _database in _e2e_setup(tmp_path):
        executor_id, credential = ids["executorId"], ids["credential"]
        heartbeat(device_client, credential, capabilities=AS_CAPABILITIES,
                  client_version=CLIENT_VERSION)
        created = web_client.post(
            "/v1/operation-runs/after-sale-claim",
            json={
                "idempotencyKey": "as-claim-e2e-00000002",
                "executorId": executor_id,
                "items": [{"environmentSerial": "4902", "orderNo": "GSH0001",
                           "storeName": "合成店铺-4902", "packageNo": ""}],
            },
            headers=CSRF,
        )
        assert created.status_code == 202, created.text
        run_id = created.json()["data"]["runId"]
        task_id, lease_token = _lease_and_start(
            device_client, credential, expect_type="after.sale.claim.v1")

        outside = device_client.post(
            f"/v1/executor-channel/tasks/{task_id}/progress",
            json={
                "leaseToken": lease_token,
                "phase": "after_sale.claim.running",
                "current": 1,
                "total": 1,
                "snapshot": {"rows": [_claim_row("GSH9999", "4902")]},
            },
            headers=device_headers(credential),
        )
        assert outside.status_code == 422, outside.text

        mismatch = device_client.post(
            f"/v1/executor-channel/tasks/{task_id}/progress",
            json={
                "leaseToken": lease_token,
                "phase": "after_sale.claim.running",
                "current": 1,
                "total": 1,
                "snapshot": {"rows": [_claim_row("GSH0001", "8888")]},
            },
            headers=device_headers(credential),
        )
        assert mismatch.status_code == 422, mismatch.text

        snapshot = web_client.get(
            f"/v1/operation-runs/after-sale-claim/{run_id}").json()["data"]
        assert snapshot["rows"] == []
        break


# ===== 取消：裸 POST 把 stop 请求下发到任务 =====
def test_after_sale_scan_cancel_requests_stop(tmp_path) -> None:
    for web_client, device_client, ids, database in _e2e_setup(tmp_path):
        executor_id, credential = ids["executorId"], ids["credential"]
        heartbeat(device_client, credential, capabilities=AS_CAPABILITIES,
                  client_version=CLIENT_VERSION)
        created = web_client.post(
            "/v1/after-sale/scan",
            json={**_scan_body("4902"), "executorId": executor_id},
            headers=CSRF,
        )
        assert created.status_code == 202, created.text
        task_id = created.json()["data"]["taskId"]
        task_id, lease_token = _lease_and_start(
            device_client, credential, expect_type="after.sale.scan.v1")

        # 前端裸 POST：不带 body、不带 CSRF 以外的任何负载
        cancelled = web_client.post(
            f"/v1/after-sale/scan/{task_id}/cancel", headers=CSRF)
        assert cancelled.status_code == 200, cancelled.text
        data = cancelled.json()["data"]
        assert data["taskId"] == task_id
        assert data["status"] == "cancel_requested"
        assert data["summary"]["totalCount"] == 1

        with database.session_factory() as session:
            from xynigo_auth.models import ExecutorTask
            task = session.get(ExecutorTask, uuid.UUID(task_id))
            assert task is not None
            assert task.cancel_requested_at is not None
            assert task.status == "cancel_requested"

        # 执行器在租约内回终态；已终态任务再次取消：原样返回，不改写结果
        finish = device_client.post(
            f"/v1/executor-channel/tasks/{task_id}/finish",
            json={
                "leaseToken": lease_token,
                "outcome": "succeeded",
                "resultCode": "after_sale_scan_completed",
                "resultSummary": {"rows": [], "totalCount": 1,
                                  "claimableCount": 0},
            },
            headers=device_headers(credential),
        )
        assert finish.status_code == 200, finish.text
        again = web_client.post(
            f"/v1/after-sale/scan/{task_id}/cancel", headers=CSRF)
        assert again.status_code == 200, again.text
        assert again.json()["data"]["status"] == "succeeded"
        break


def test_after_sale_claim_cancel_requests_stop(tmp_path) -> None:
    for web_client, device_client, ids, _database in _e2e_setup(tmp_path):
        executor_id, credential = ids["executorId"], ids["credential"]
        heartbeat(device_client, credential, capabilities=AS_CAPABILITIES,
                  client_version=CLIENT_VERSION)
        created = web_client.post(
            "/v1/operation-runs/after-sale-claim",
            json={
                "idempotencyKey": "as-claim-e2e-00000003",
                "executorId": executor_id,
                "items": [{"environmentSerial": "4902", "orderNo": "GSH0001",
                           "storeName": "合成店铺-4902", "packageNo": ""},
                          {"environmentSerial": "4901", "orderNo": "GSH0002",
                           "storeName": "合成店铺-4901", "packageNo": ""}],
            },
            headers=CSRF,
        )
        assert created.status_code == 202, created.text
        run_id = created.json()["data"]["runId"]
        task_id, _lease_token = _lease_and_start(
            device_client, credential, expect_type="after.sale.claim.v1")

        cancelled = web_client.post(
            f"/v1/operation-runs/after-sale-claim/{run_id}/cancel", headers=CSRF)
        assert cancelled.status_code == 200, cancelled.text
        data = cancelled.json()["data"]
        assert data["stopRequested"] is True
        assert data["status"] == "running"
        assert data["progressTotal"] == 2

        # 未知批次：404
        missing = web_client.post(
            f"/v1/operation-runs/after-sale-claim/{uuid.uuid4()}/cancel",
            headers=CSRF)
        assert missing.status_code == 404, missing.text
        assert web_client.get(
            f"/v1/operation-runs/after-sale-claim/{uuid.uuid4()}"
        ).status_code == 404
        break
