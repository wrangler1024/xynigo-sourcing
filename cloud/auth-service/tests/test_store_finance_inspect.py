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


def test_store_finance_progress_accepts_production_snapshot_shape(tmp_path) -> None:
    """必须改1回归（二轮）：执行器真实 snapshot 形状——行带 screenshotStatus、
    采集附加金额字段、None 值；截图附件带 contentType+size——必须 200 且落库，
    不允许 extra=forbid 触发 422 断流。"""
    for web_client, device_client, ids, database in _e2e_setup(tmp_path):
        executor_id = ids["executorId"]
        credential = ids["credential"]
        heartbeat(device_client, credential, capabilities=SF_CAPABILITIES,
                  client_version="0.17.18")
        created = web_client.post(
            "/v1/operation-runs/store-finance-inspect",
            json={
                "idempotencyKey": "store-finance-e2e-00000003",
                "executorId": executor_id,
                "environmentSerials": ["1746"],
            },
            headers=CSRF,
        )
        assert created.status_code == 202, created.text
        run_id = created.json()["data"]["runId"]
        lease = heartbeat(device_client, credential,
                          capabilities=SF_CAPABILITIES,
                          client_version="0.17.18")["task"]
        task_id = lease["id"]
        lease_token = lease["leaseToken"]
        device_client.post(
            f"/v1/executor-channel/tasks/{task_id}/start",
            json={"leaseToken": lease_token},
            headers=device_headers(credential),
        )

        # 生产 snapshot 形状：StoreFinanceInspector.snapshot() 原样输出，
        # 含 screenshotStatus / cumulativeSettlementAmount / fundLimitAmount /
        # payoutInProgressAmount / errorSummary=None / screenshotSha256=None
        production_row = {
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
            "pendingSettleLimitAmount": 0.0,
            "lastPayoutAmount": 11426.0,
            "withdrawableAmount": 0.0,
            "cumulativeSettlementAmount": 37884.53,   # 本地附加字段
            "fundLimitAmount": 237.05,                # 本地附加字段
            "payoutInProgressAmount": 0.0,            # 本地附加字段
            "collectedAt": "2026-09-10T14:20:00+08:00",
            "durationSeconds": 95,
            "errorSummary": None,                     # None 值
            "screenshotSha256": None,                 # None 值
            "screenshotStatus": "",                   # 本地附加字段
        }
        # 生产截图附件形状：带 contentType + size（物流同形）
        production_attachment = {
            "environmentSerial": "1746",
            "contentBase64": base64.b64encode(JPEG).decode(),
            "sha256": hashlib.sha256(JPEG).hexdigest(),
            "contentType": "image/jpeg",
            "size": len(JPEG),
        }
        progress = device_client.post(
            f"/v1/executor-channel/tasks/{task_id}/progress",
            json={
                "leaseToken": lease_token,
                "phase": "store_finance.running",
                "current": 1,
                "total": 1,
                "snapshot": {
                    "rows": [production_row],
                    "screenshots": [production_attachment],
                },
            },
            headers=device_headers(credential),
        )
        # 修复前此处 422（screenshotStatus 等多余字段触发 extra_forbidden）
        assert progress.status_code == 200, progress.text

        finish = device_client.post(
            f"/v1/executor-channel/tasks/{task_id}/finish",
            json={
                "leaseToken": lease_token,
                "outcome": "succeeded",
                "resultCode": "store_finance_completed",
                "resultSummary": {
                    "runStatus": "completed",
                    "phase": "store_finance.completed",
                    "progressCompleted": 1,
                    "progressTotal": 1,
                    "successCount": 1,
                    "failedCount": 0,
                },
            },
            headers=device_headers(credential),
        )
        assert finish.status_code == 200, finish.text

        final = web_client.get(
            f"/v1/operation-runs/store-finance-inspect/{run_id}"
        )
        assert final.status_code == 200
        data = final.json()["data"]
        assert data["status"] == "completed"
        assert len(data["rows"]) == 1
        row = data["rows"][0]
        assert row["storeName"] == "山岚"
        assert row["inTransitAmount"] == 3031.29
        assert row["nonWithdrawableAmount"] == 237.05

        # 截图二进制已落库（带 contentType+size 的附件形状）
        with database.session_factory() as session:
            from sqlalchemy import select
            from xynigo_auth.models import StoreFinanceInspectResult
            stored = session.scalar(
                select(StoreFinanceInspectResult).where(
                    StoreFinanceInspectResult.run_id
                    == uuid.UUID(run_id),
                )
            )
            assert stored is not None
            assert stored.screenshot_content == JPEG
            assert stored.screenshot_expires_at is not None


def test_store_finance_progress_mixed_queued_and_ok_rows(tmp_path) -> None:
    """三轮评审必须改1回归：queued 行（无 loginMode）投影后 loginMode=None
    必须通过校验，整份 progress 不得被拒；GET 能同时看到 queued 与 ok 行。"""
    for web_client, device_client, ids, database in _e2e_setup(tmp_path):
        heartbeat(device_client, ids["credential"],
                  capabilities=SF_CAPABILITIES, client_version="0.17.18")
        created = web_client.post(
            "/v1/operation-runs/store-finance-inspect",
            json={
                "idempotencyKey": "store-finance-e2e-00000004",
                "executorId": ids["executorId"],
                "environmentSerials": ["1746", "1775875785"],
            },
            headers=CSRF,
        )
        assert created.status_code == 202, created.text
        run_id = created.json()["data"]["runId"]
        lease = heartbeat(device_client, ids["credential"],
                          capabilities=SF_CAPABILITIES,
                          client_version="0.17.18")["task"]
        task_id, lease_token = lease["id"], lease["leaseToken"]
        device_client.post(
            f"/v1/executor-channel/tasks/{task_id}/start",
            json={"leaseToken": lease_token},
            headers=device_headers(ids["credential"]),
        )
        # 投影后的混合行：ok 行全字段、queued 行 loginMode=None 且仅闭集键
        progress = device_client.post(
            f"/v1/executor-channel/tasks/{task_id}/progress",
            json={
                "leaseToken": lease_token,
                "phase": "store_finance.running",
                "current": 1,
                "total": 2,
                "snapshot": {"rows": [
                    {"environmentSerial": "1746", "storeName": "山岚",
                     "gsCode": "GS2392643", "status": "ok",
                     "loginMode": "auto", "inTransitAmount": 3031.29,
                     "unsettledAmount": 7286.45,
                     "nextSettlementAmount": 3080.5,
                     "nextSettlementDate": "2026-09-15",
                     "completedSettlementAmount": 37884.53,
                     "nonWithdrawableAmount": 237.05,
                     "collectedAt": "2026-09-10T14:20:00+08:00",
                     "durationSeconds": 95,
                     "errorSummary": None, "screenshotSha256": None},
                    {"environmentSerial": "1775875785",
                     "storeName": "花间", "gsCode": "GS5021497",
                     "status": "queued", "loginMode": None},
                ]},
            },
            headers=device_headers(ids["credential"]),
        )
        # 修复前：queued 行 loginMode="" 触发 literal_error → 整份 422
        assert progress.status_code == 200, progress.text

        final = web_client.get(
            f"/v1/operation-runs/store-finance-inspect/{run_id}"
        )
        assert final.status_code == 200
        rows = {r["environmentSerial"]: r for r in final.json()["data"]["rows"]}
        assert rows["1746"]["status"] == "ok"
        assert rows["1775875785"]["status"] == "queued"
        assert rows["1775875785"]["loginMode"] is None


# ===== 环境查询（store.finance.lookup.v1）：粘贴 → HubStudio 解析 → 合并上次采集 =====
def test_store_finance_environment_lookup_round_trip(tmp_path) -> None:
    import uuid as uuid_module
    from sqlalchemy import select
    from xynigo_auth.models import (
        StoreFinanceInspectResult,
        StoreFinanceInspectRun,
    )

    capabilities = SF_CAPABILITIES + ["store.finance.lookup.v1"]
    for web_client, device_client, ids, database in _e2e_setup(tmp_path):
        executor_id = ids["executorId"]
        credential = ids["credential"]
        heartbeat(device_client, credential, capabilities=capabilities,
                  client_version="0.17.18")

        created = web_client.post(
            "/v1/store-finance-environments/lookup",
            json={
                "executorId": executor_id,
                "identifiers": ["溪山", "1377", "不存在"],
                "idempotencyKey": "sf-lookup-e2e-00001",
            },
            headers=CSRF,
        )
        assert created.status_code == 202, created.text
        task_id = created.json()["data"]["taskId"]

        lease = heartbeat(device_client, credential,
                          capabilities=capabilities,
                          client_version="0.17.18")["task"]
        assert lease is not None and lease["type"] == \
            "store.finance.lookup.v1", lease
        lease_token = lease["leaseToken"]
        assert device_client.post(
            f"/v1/executor-channel/tasks/{lease['id']}/start",
            json={"leaseToken": lease_token},
            headers=device_headers(credential),
        ).status_code == 200

        finish = device_client.post(
            f"/v1/executor-channel/tasks/{lease['id']}/finish",
            json={
                "leaseToken": lease_token,
                "outcome": "succeeded",
                "resultCode": "store_finance_lookup_completed",
                "resultSummary": {
                    "matched": [{
                        "environmentSerial": "1377",
                        "environmentId": "1776003960",
                        "storeName": "溪山-子",
                        "gsCode": "GS1098478",
                        "group": "魏无羡",
                        "browserOpen": False,
                    }],
                    "unmatched": ["不存在"],
                },
            },
            headers=device_headers(credential),
        )
        assert finish.status_code == 200, finish.text

        # 先跑一次真实巡检链路，为 GS1098478 沉淀「上次采集/登录态」
        inspect_created = web_client.post(
            "/v1/operation-runs/store-finance-inspect",
            json={
                "idempotencyKey": "sf-lookup-history-00000001",
                "executorId": executor_id,
                "environmentSerials": ["1377"],
            },
            headers=CSRF,
        )
        assert inspect_created.status_code == 202, inspect_created.text
        inspect_lease = heartbeat(device_client, credential,
                                  capabilities=capabilities,
                                  client_version="0.17.18")["task"]
        assert inspect_lease["type"] == "store.finance.inspect.v1"
        device_client.post(
            f"/v1/executor-channel/tasks/{inspect_lease['id']}/start",
            json={"leaseToken": inspect_lease["leaseToken"]},
            headers=device_headers(credential),
        )
        progress_resp = device_client.post(
            f"/v1/executor-channel/tasks/{inspect_lease['id']}/progress",
            json={
                "leaseToken": inspect_lease["leaseToken"],
                "phase": "store_finance.running",
                "current": 1,
                "total": 1,
                "snapshot": {"rows": [{
                    "environmentSerial": "1377",
                    "storeName": "溪山-子",
                    "gsCode": "GS1098478",
                    "status": "ok",
                    "loginMode": "reuse",
                    "inTransitAmount": 10.0,
                    "unsettledAmount": None,
                    "nextSettlementAmount": None,
                    "nextSettlementDate": "",
                    "completedSettlementAmount": None,
                    "nonWithdrawableAmount": None,
                    "pendingSettleLimitAmount": None,
                    "lastPayoutAmount": None,
                    "withdrawableAmount": None,
                    "collectedAt": "2026-09-10T14:20:00+08:00",
                    "durationSeconds": 60,
                    "errorSummary": None,
                    "screenshotSha256": None,
                }]},
            },
            headers=device_headers(credential),
        )
        assert progress_resp.status_code == 200, progress_resp.text
        finish_resp = device_client.post(
            f"/v1/executor-channel/tasks/{inspect_lease['id']}/finish",
            json={
                "leaseToken": inspect_lease["leaseToken"],
                "outcome": "succeeded",
                "resultCode": "store_finance_completed",
                "resultSummary": {
                    "runStatus": "completed",
                    "phase": "store_finance.completed",
                    "progressCompleted": 1,
                    "progressTotal": 1,
                    "successCount": 1,
                    "failedCount": 0,
                    "stoppedCount": 0,
                },
            },
            headers=device_headers(credential),
        )

        fetched = web_client.get(
            f"/v1/store-finance-environments/lookup/{task_id}")
        assert fetched.status_code == 200, fetched.text
        data = fetched.json()["data"]
        assert data["status"] == "succeeded"
        row = data["matched"][0]
        assert row["environmentSerial"] == "1377"
        assert row["storeName"] == "溪山-子"
        assert "remark" not in row  # 云端输出闭集，多余字段裁剪
        assert data["unmatched"] == ["不存在"]
        assert data["lastRuns"]["GS1098478"]["status"] == "ok"
        assert data["lastRuns"]["GS1098478"]["loginMode"] == "reuse"
        break


# ===== 补采合并：failed_retry Run 的快照/导出合并源 Run 成功行 =====
def test_store_finance_retry_run_merges_source_rows(tmp_path) -> None:
    for web_client, device_client, ids, database in _e2e_setup(tmp_path):
        executor_id = ids["executorId"]
        credential = ids["credential"]
        heartbeat(device_client, credential, capabilities=SF_CAPABILITIES,
                  client_version="0.17.18")

        # 源批次：山岚 ok + 花间 fail
        created = web_client.post(
            "/v1/operation-runs/store-finance-inspect",
            json={
                "idempotencyKey": "sf-merge-source-00001",
                "executorId": executor_id,
                "environmentSerials": ["1746", "1747"],
            },
            headers=CSRF,
        )
        assert created.status_code == 202, created.text
        source_run_id = created.json()["data"]["runId"]
        lease = heartbeat(device_client, credential,
                          capabilities=SF_CAPABILITIES,
                          client_version="0.17.18")["task"]
        lease_token = lease["leaseToken"]
        device_client.post(
            f"/v1/executor-channel/tasks/{lease['id']}/start",
            json={"leaseToken": lease_token},
            headers=device_headers(credential),
        )

        def _row(serial, name, status):
            return {
                "environmentSerial": serial,
                "storeName": name,
                "gsCode": "GS" + serial,
                "status": status,
                "loginMode": "auto" if status == "ok" else None,
                "inTransitAmount": 10.0 if status == "ok" else None,
                "unsettledAmount": None,
                "nextSettlementAmount": None,
                "nextSettlementDate": "",
                "completedSettlementAmount": None,
                "nonWithdrawableAmount": None,
                "pendingSettleLimitAmount": None,
                "lastPayoutAmount": None,
                "withdrawableAmount": None,
                "collectedAt": "2026-09-11T06:00:00+08:00",
                "durationSeconds": 30,
                "errorSummary": None if status == "ok" else "失败原因",
                "screenshotSha256": None,
            }

        device_client.post(
            f"/v1/executor-channel/tasks/{lease['id']}/progress",
            json={
                "leaseToken": lease_token,
                "phase": "store_finance.running",
                "current": 2,
                "total": 2,
                "snapshot": {"rows": [
                    _row("1746", "山岚", "ok"),
                    _row("1747", "花间", "fail"),
                ]},
            },
            headers=device_headers(credential),
        )
        device_client.post(
            f"/v1/executor-channel/tasks/{lease['id']}/finish",
            json={
                "leaseToken": lease_token,
                "outcome": "succeeded",
                "resultCode": "store_finance_partial_failure",
                "resultSummary": {
                    "runStatus": "partial_failure",
                    "phase": "store_finance.partial_failure",
                    "progressCompleted": 2,
                    "progressTotal": 2,
                    "successCount": 1,
                    "failedCount": 1,
                    "stoppedCount": 0,
                },
            },
            headers=device_headers(credential),
        )
        source_snap = web_client.get(
            f"/v1/operation-runs/store-finance-inspect/{source_run_id}"
        ).json()["data"]
        assert source_snap["successCount"] == 1

        # 补采批次：只重跑花间(1747)，标记 sourceRunId
        retry = web_client.post(
            "/v1/operation-runs/store-finance-inspect",
            json={
                "idempotencyKey": "sf-merge-retry-00001",
                "executorId": executor_id,
                "queryMode": "failed_retry",
                "environmentSerials": ["1747"],
                "sourceRunId": source_run_id,
            },
            headers=CSRF,
        )
        assert retry.status_code == 202, retry.text
        retry_run_id = retry.json()["data"]["runId"]
        # 发起瞬间快照就应带出源 Run 的成功行（汇总不清空）
        early = web_client.get(
            f"/v1/operation-runs/store-finance-inspect/{retry_run_id}"
        ).json()["data"]
        assert {r["storeName"] for r in early["rows"]} == {"山岚", "花间"}
        assert early["totalCount"] == 2
        # 进度条分母=合并总数，分子=源成功数（发起即 1/2，而非 0/1）
        assert early["progressTotal"] == 2
        assert early["progressCompleted"] == 1

        lease2 = heartbeat(device_client, credential,
                           capabilities=SF_CAPABILITIES,
                           client_version="0.17.18")["task"]
        lease2_token = lease2["leaseToken"]
        device_client.post(
            f"/v1/executor-channel/tasks/{lease2['id']}/start",
            json={"leaseToken": lease2_token},
            headers=device_headers(credential),
        )
        device_client.post(
            f"/v1/executor-channel/tasks/{lease2['id']}/progress",
            json={
                "leaseToken": lease2_token,
                "phase": "store_finance.running",
                "current": 1,
                "total": 1,
                "snapshot": {"rows": [
                    {**_row("1747", "花间", "ok"),
                     "inTransitAmount": 99.0},
                ]},
            },
            headers=device_headers(credential),
        )
        device_client.post(
            f"/v1/executor-channel/tasks/{lease2['id']}/finish",
            json={
                "leaseToken": lease2_token,
                "outcome": "succeeded",
                "resultCode": "store_finance_completed",
                "resultSummary": {
                    "runStatus": "completed",
                    "phase": "store_finance.completed",
                    "progressCompleted": 1,
                    "progressTotal": 1,
                    "successCount": 1,
                    "failedCount": 0,
                    "stoppedCount": 0,
                },
            },
            headers=device_headers(credential),
        )
        final = web_client.get(
            f"/v1/operation-runs/store-finance-inspect/{retry_run_id}"
        ).json()["data"]
        # 合并后：两家全 ok，花间金额来自补采行
        rows = {r["storeName"]: r for r in final["rows"]}
        assert rows["山岚"]["status"] == "ok"
        assert rows["花间"]["status"] == "ok"
        assert rows["花间"]["inTransitAmount"] == 99.0
        assert final["successCount"] == 2
        assert final["failedCount"] == 0
        assert final["mergedFromSource"] is True
        assert isinstance(final["sourceElapsedSeconds"], int)
        # 补采完成后进度走满：2/2
        assert final["progressTotal"] == 2
        assert final["progressCompleted"] == 2

        exported = web_client.get(
            f"/v1/operation-runs/store-finance-inspect/{retry_run_id}/export"
        )
        assert exported.status_code == 200, exported.text
        values = _sheet_values(exported.content)
        names = {row[0] for row in values[1:] if row[0] != "合计"}
        assert names == {"山岚", "花间"}  # 导出含源 Run 成功行
        break
