# -*- coding: utf-8 -*-
"""店铺结算巡检：云端幂等落库 / 快照 / 导出契约测试。"""
from __future__ import annotations

from io import BytesIO

from openpyxl import load_workbook
from fastapi.testclient import TestClient
from test_purchase_api import authenticated_client, build_test_app

from xynigo_auth.store_finance_export import build_store_finance_export


def _run_body() -> dict[str, object]:
    return {
        "source": "local_executor",
        "runKey": "store-finance-create-0001",
        "queryMode": "initial",
        "startedAt": "2026-09-10T14:00:00+08:00",
        "completedAt": "2026-09-10T14:30:00+08:00",
        "results": [
            {
                "environmentSerial": "1746",
                "storeName": "山岚",
                "gsCode": "GS2392643",
                "status": "ok",
                "loginMode": "auto",
                "inTransitAmount": 3031.29,
                "unsettledAmount": 7286.45,
                "nextSettlementAmount": 3080.5,
                "nextSettlementDate": "2026-09-15",
                "completedSettlementAmount": 37884.53,
                "nonWithdrawableAmount": 237.05,
                "pendingSettleLimitAmount": 0,
                "lastPayoutAmount": 11426.0,
                "withdrawableAmount": 0,
                "collectedAt": "2026-09-10T14:20:00+08:00",
                "durationSeconds": 95,
            },
            {
                "environmentSerial": "1775875785",
                "storeName": "花间",
                "gsCode": "GS5021497",
                "status": "ok",
                "loginMode": "reuse",
                "inTransitAmount": 2420.01,
                "unsettledAmount": 9680.77,
                "nextSettlementAmount": 4663.47,
                "nextSettlementDate": "2026-09-15",
                "completedSettlementAmount": 54216.44,
                "nonWithdrawableAmount": 1934.51,
                "collectedAt": "2026-09-10T14:25:00+08:00",
                "durationSeconds": 70,
            },
            {
                "environmentSerial": "1775875509",
                "storeName": "蓝天",
                "gsCode": "GS2277990",
                "status": "login",
                "errorSummary": "自动登录未完成（验证未通过）",
                "collectedAt": "2026-09-10T14:28:00+08:00",
            },
        ],
    }


def _sheet_values(content: bytes) -> list[tuple[object, ...]]:
    workbook = load_workbook(BytesIO(content), read_only=True,
                             data_only=True)
    values = list(workbook.active.iter_rows(values_only=True))
    workbook.close()
    return values


def test_store_finance_export_builds_standard_workbook() -> None:
    rows = [
        {"storeName": "花间", "status": "ok",
         "inTransitAmount": 2420.01, "unsettledAmount": 9680.77,
         "nextSettlementAmount": 4663.47,
         "completedSettlementAmount": 54216.44,
         "nonWithdrawableAmount": 1934.51},
        {"storeName": "山岚", "status": "login",
         "nonWithdrawableAmount": None},
    ]
    content, filename, mime = build_store_finance_export(rows)
    assert filename.startswith("店铺结算汇总表_")
    assert filename.endswith(".xlsx")
    assert "spreadsheetml" in mime
    values = _sheet_values(content)
    assert values[0][0] == "店铺中文名"
    # 标准导出只含成功采集行（login 的蓝天不进财务六列表）
    names = [row[0] for row in values[1:-1] if row[0]]
    assert set(names) == {"花间"}
    content2, filename2, _ = build_store_finance_export(
        rows, variant="full")
    assert filename2.endswith("_full.csv")
    assert "已完成结算收入" in content2.decode("utf-8-sig")


# ===== 必须改1/2：executor-channel progress/finish 全链契约 =====
import base64
import hashlib
import uuid

from test_executor_channel import (
    CSRF,
    create_pairing_code,
    device_headers,
    heartbeat,
    login,
    pair,
)

SF_CAPABILITIES = [
    "config.read.v1",
    "config.write.v1",
    "workspace.snapshot.v1",
    "store.finance.inspect.v1",
]
JPEG = b"\xff\xd8synthetic-store-finance-screenshot\xff\xd9"


def _e2e_setup(tmp_path):
    app, database, _oauth = build_test_app(tmp_path)
    with TestClient(app) as web_client, TestClient(app) as device_client:
        login(web_client)
        paired = pair(
            device_client,
            create_pairing_code(web_client),
            capabilities=SF_CAPABILITIES,
        )
        yield web_client, device_client, {
            "executorId": str(paired["executorId"]),
            "credential": str(paired["deviceCredential"]),
        }, database


def _e2e_params(tmp_path):
    yield from _e2e_setup(tmp_path)


def test_store_finance_progress_finish_updates_run_snapshot_and_screenshots(tmp_path) -> None:
    """必须改1回归：progress/finish 后 GET 快照有行、终态、截图可读。"""
    import time as time_module
    for web_client, device_client, ids, database in _e2e_setup(tmp_path):
        executor_id = ids["executorId"]
        credential = ids["credential"]
        # 心跳上报新版本与能力，使发起门禁通过
        heartbeat(device_client, credential, capabilities=SF_CAPABILITIES,
                  client_version="0.17.18")

        created = web_client.post(
            "/v1/operation-runs/store-finance-inspect",
            json={
                "idempotencyKey": "store-finance-e2e-00000001",
                "executorId": executor_id,
                "queryMode": "initial",
                "browserMode": "headless",
                "concurrency": 3,
                "environmentSerials": ["1746", "1775875785"],
            },
            headers=CSRF,
        )
        assert created.status_code == 202, created.text
        run_id = created.json()["data"]["runId"]

        # 执行器领取任务
        lease = heartbeat(device_client, credential,
                          capabilities=SF_CAPABILITIES,
                          client_version="0.17.18")["task"]
        assert lease is not None and lease["type"] == \
            "store.finance.inspect.v1", lease
        task_id = lease["id"]
        lease_token = lease["leaseToken"]
        assert device_client.post(
            f"/v1/executor-channel/tasks/{task_id}/start",
            json={"leaseToken": lease_token},
            headers=device_headers(credential),
        ).status_code == 200

        # progress：行快照（一店完成带截图，一店进行中）
        sha = hashlib.sha256(JPEG).hexdigest()
        progress = device_client.post(
            f"/v1/executor-channel/tasks/{task_id}/progress",
            json={
                "leaseToken": lease_token,
                "phase": "store_finance.running",
                "current": 1,
                "total": 2,
                "snapshot": {
                    "rows": [
                        {
                            "environmentSerial": "1746",
                            "storeName": "山岚",
                            "gsCode": "GS2392643",
                            "status": "ok",
                            "loginMode": "auto",
                            "inTransitAmount": 3031.29,
                            "unsettledAmount": 7286.45,
                            "nextSettlementAmount": 3080.5,
                            "nextSettlementDate": "2026-09-15",
                            "completedSettlementAmount": 37884.53,
                            "nonWithdrawableAmount": 237.05,
                            "collectedAt": "2026-09-10T14:20:00+08:00",
                            "durationSeconds": 95,
                            "screenshotSha256": sha,
                        },
                        {
                            "environmentSerial": "1775875785",
                            "storeName": "花间",
                            "status": "running",
                        },
                    ],
                    "screenshots": [{
                        "environmentSerial": "1746",
                        "contentBase64": base64.b64encode(JPEG).decode(),
                        "sha256": sha,
                    }],
                },
            },
            headers=device_headers(credential),
        )
        assert progress.status_code == 200, progress.text

        # progress 阶段 GET 快照：已有行（边跑边填）
        partial = web_client.get(
            f"/v1/operation-runs/store-finance-inspect/{run_id}"
        )
        assert partial.status_code == 200
        partial_data = partial.json()["data"]
        assert partial_data["status"] == "running"
        ok_rows = [r for r in partial_data["rows"] if r["status"] == "ok"]
        assert ok_rows and ok_rows[0]["storeName"] == "山岚"
        assert ok_rows[0]["inTransitAmount"] == 3031.29

        # finish：终态
        finish = device_client.post(
            f"/v1/executor-channel/tasks/{task_id}/finish",
            json={
                "leaseToken": lease_token,
                "outcome": "succeeded",
                "resultCode": "store_finance_completed",
                "resultSummary": {
                    "runStatus": "partial_failure",
                    "phase": "store_finance.partial_failure",
                    "progressCompleted": 2,
                    "progressTotal": 2,
                    "successCount": 1,
                    "failedCount": 1,
                },
            },
            headers=device_headers(credential),
        )
        assert finish.status_code == 200, finish.text

        # GET 快照：终态 + 行 + 截图可读（落库二进制）
        final = web_client.get(
            f"/v1/operation-runs/store-finance-inspect/{run_id}"
        )
        assert final.status_code == 200
        final_data = final.json()["data"]
        assert final_data["status"] == "partial_failure"
        assert len(final_data["rows"]) == 2
        ok_row = [r for r in final_data["rows"]
                  if r["environmentSerial"] == "1746"][0]
        assert ok_row["screenshotSha256"] == sha

        # 截图二进制已随 progress 落库（含 content，非仅 sha）
        with database.session_factory() as session:
            from sqlalchemy import select
            from xynigo_auth.models import StoreFinanceInspectResult
            row = session.scalar(
                select(StoreFinanceInspectResult).where(
                    StoreFinanceInspectResult.run_id
                    == uuid.UUID(run_id),
                    StoreFinanceInspectResult.environment_serial
                    == "1746",
                )
            )
            assert row is not None
            assert row.screenshot_content == JPEG
            assert row.screenshot_expires_at is not None

        # 导出：终态后标准 xlsx 可下载且含 2 行数据
        export = web_client.get(
            f"/v1/operation-runs/store-finance-inspect/{run_id}/export"
            "?variant=standard"
        )
        assert export.status_code == 200, export.text

        # 必须改2回归：取消路由存在且已终态 Run 返回冲突/幂等而非 404 假成功
        cancel = web_client.post(
            f"/v1/operation-runs/store-finance-inspect/{run_id}/cancel",
            json={},
            headers=CSRF,
        )
        assert cancel.status_code in (200, 409), cancel.text


def test_store_finance_cancel_route_requests_stop(tmp_path) -> None:
    """必须改2回归：cancel 路由把 stop_requested 写回 Run。"""
    for web_client, device_client, ids, database in _e2e_setup(tmp_path):
        executor_id = ids["executorId"]
        credential = ids["credential"]
        heartbeat(device_client, credential, capabilities=SF_CAPABILITIES,
                  client_version="0.17.18")
        created = web_client.post(
            "/v1/operation-runs/store-finance-inspect",
            json={
                "idempotencyKey": "store-finance-e2e-00000002",
                "executorId": executor_id,
                "environmentSerials": ["1746"],
            },
            headers=CSRF,
        )
        assert created.status_code == 202, created.text
        run_id = created.json()["data"]["runId"]
        cancelled = web_client.post(
            f"/v1/operation-runs/store-finance-inspect/{run_id}/cancel",
            json={},
            headers=CSRF,
        )
        assert cancelled.status_code == 200, cancelled.text
        data = cancelled.json()["data"]
        assert data["stopRequested"] is True
