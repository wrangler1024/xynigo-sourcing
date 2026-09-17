# -*- coding: utf-8 -*-
"""平台查找（refund_discovery）云端契约测试：能力门禁、任务派发、
进度/最终结果闭集校验、发现身份历史核对与导出防串。

只用合成数据：不连浏览器、不写真实平台、不虚构系统提交记录。
"""
from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from xynigo_auth.models import (
    AfterSaleClaimResult, AfterSaleClaimRun, AfterSaleRefundTracking, Tenant,
)
from test_after_sale_claim import _e2e_setup, AS_CAPABILITIES, CLIENT_VERSION
from test_executor_channel import CSRF, device_headers, heartbeat

DISCOVERY_CAPABILITIES = [*AS_CAPABILITIES, "after.sale.refund-discovery.v1"]


@pytest.fixture
def context(tmp_path):
    yield from _e2e_setup(tmp_path, capabilities=DISCOVERY_CAPABILITIES)


def _discovery_row(serial: str, order_no: str, bills: list[str], *,
                   environment_status: str = "ok",
                   status: str = "ok",
                   claimable: bool = False,
                   package_count: int = 0) -> dict[str, object]:
    row = {
        "environmentSerial": serial,
        "storeName": "合成环境-" + serial,
        "accountName": "buyer@example.test",
        "orderNo": order_no,
        "deliveredAt": "",
        "amount": "",
        "status": status,
        "claimable": claimable,
        "packageCount": package_count,
        "trackingNo": "",
        "errorSummary": None,
        "screenshotSha256": None,
        "refundBillIds": bills,
        "environmentStatus": environment_status,
    }
    return row


def _create_task(web_client, executor_id, serials, *, purpose="refund_discovery",
                 key="as-discovery-e2e-0001"):
    return web_client.post(
        "/v1/after-sale/scan",
        json={
            "idempotencyKey": key,
            "executorId": executor_id,
            "browserMode": "headless",
            "concurrency": 2,
            "environmentSerials": serials,
            "purpose": purpose,
        },
        headers=CSRF,
    )


def _lease_and_start(device_client, credential, *, expect_type="after.sale.scan.v1"):
    lease = heartbeat(device_client, credential,
                      capabilities=DISCOVERY_CAPABILITIES,
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


def _progress(device_client, credential, task_id, lease_token, rows, *,
              current=None, total=None, phase="after_sale.discovery.running"):
    body = {"leaseToken": lease_token, "phase": phase,
            "snapshot": {"rows": rows}}
    if current is not None:
        body["current"] = current
    if total is not None:
        body["total"] = total
    return device_client.post(
        f"/v1/executor-channel/tasks/{task_id}/progress",
        json=body, headers=device_headers(credential))


def test_discovery_task_requires_new_capability(tmp_path) -> None:
    """无 after.sale.refund-discovery.v1 的执行器被 409 拦下；普通扫描不受影响。"""
    for web_client, device_client, ids, _database in _e2e_setup(tmp_path):
        executor_id, credential = ids["executorId"], ids["credential"]
        heartbeat(device_client, credential, capabilities=AS_CAPABILITIES,
                  client_version=CLIENT_VERSION)
        blocked = _create_task(web_client, executor_id, ["4902"])
        assert blocked.status_code == 409, blocked.text
        assert blocked.json()["detail"]["code"] == (
            "executor_after_sale_discovery_upgrade_required")
        # 旧入口（不传 purpose）不因新门禁误伤
        legacy = web_client.post(
            "/v1/after-sale/scan",
            json={"idempotencyKey": "as-scan-legacy-000001",
                  "executorId": executor_id, "browserMode": "headless",
                  "concurrency": 2, "environmentSerials": ["4902"]},
            headers=CSRF)
        assert legacy.status_code == 202, legacy.text
        break


def test_discovery_progress_and_terminal_summary(context) -> None:
    web_client, device_client, ids, _database = context
    executor_id, credential = ids["executorId"], ids["credential"]
    heartbeat(device_client, credential, capabilities=DISCOVERY_CAPABILITIES,
              client_version=CLIENT_VERSION)
    created = _create_task(web_client, executor_id, ["4902", "4901"],
                           key="as-discovery-flow-00001")
    assert created.status_code == 202, created.text
    task_id = created.json()["data"]["taskId"]
    # 普通扫描的 GET 响应形状不受新用途影响：只有发现任务才带 purpose 键
    early = web_client.get(f"/v1/after-sale/scan/{task_id}").json()["data"]
    assert early["summary"]["purpose"] == "refund_discovery"
    assert early["summary"]["progressTotal"] == 2

    task_id2, lease_token = _lease_and_start(device_client, credential)
    assert task_id2 == task_id

    # 部分进度：一个环境完成（含两个退款单），另一个仍在查找
    partial = _progress(device_client, credential, task_id, lease_token, [
        _discovery_row("4902", "GSH0001", ["2390000000000001", "2390000000000002"]),
        _discovery_row("4902", "GSH0002", []),
        _discovery_row("4901", "", [], environment_status="running",
                       status="running"),
    ], current=1, total=2)
    assert partial.status_code == 200, partial.text
    running = web_client.get(f"/v1/after-sale/scan/{task_id}").json()["data"]
    assert running["summary"]["progressCompleted"] == 1
    assert running["summary"]["refundCount"] == 2
    assert running["summary"]["claimableCount"] == 0

    final_rows = [
        _discovery_row("4902", "GSH0001", ["2390000000000001", "2390000000000002"]),
        _discovery_row("4902", "GSH0002", []),
        _discovery_row("4901", "", [], status="empty"),
    ]
    finish = device_client.post(
        f"/v1/executor-channel/tasks/{task_id}/finish",
        json={
            "leaseToken": lease_token,
            "outcome": "succeeded",
            "resultCode": "after_sale_discovery_completed",
            "resultSummary": {
                "rows": final_rows,
                "totalCount": 2,
                "claimableCount": 0,
                "refundCount": 2,
                "progressCompleted": 2,
                "progressTotal": 2,
            },
        },
        headers=device_headers(credential),
    )
    assert finish.status_code == 200, finish.text
    done = web_client.get(f"/v1/after-sale/scan/{task_id}").json()["data"]
    assert done["status"] == "succeeded"
    assert done["summary"]["progressCompleted"] == 2
    assert done["summary"]["refundCount"] == 2
    # 发现任务不导出可申请清单
    export = web_client.get(f"/v1/after-sale/scan/{task_id}/export")
    assert export.status_code == 409, export.text


def test_discovery_progress_rejects_out_of_scope_and_dirty_semantics(context) -> None:
    web_client, device_client, ids, _database = context
    executor_id, credential = ids["executorId"], ids["credential"]
    heartbeat(device_client, credential, capabilities=DISCOVERY_CAPABILITIES,
              client_version=CLIENT_VERSION)
    task_id = _create_task(web_client, executor_id, ["4902"],
                           key="as-discovery-reject-0001").json()["data"]["taskId"]
    task_id2, lease_token = _lease_and_start(device_client, credential)
    assert task_id2 == task_id

    out_of_scope = _progress(device_client, credential, task_id, lease_token,
                             [_discovery_row("4999", "GSH0001", ["1"])])
    assert out_of_scope.status_code == 422
    assert out_of_scope.json()["detail"]["code"] == "executor_progress_snapshot_invalid"

    duplicated = _progress(device_client, credential, task_id, lease_token, [
        _discovery_row("4902", "GSH0001", ["2390000000000001"]),
        _discovery_row("4902", "GSH0002", ["2390000000000001"]),
    ])
    assert duplicated.status_code == 422
    assert duplicated.json()["detail"]["code"] == "executor_progress_snapshot_invalid"

    claimable = _progress(device_client, credential, task_id, lease_token, [
        _discovery_row("4902", "GSH0001", ["2390000000000001"],
                       claimable=True, package_count=2),
    ])
    assert claimable.status_code == 422
    assert claimable.json()["detail"]["code"] == "executor_progress_snapshot_invalid"

    inconsistent = _progress(device_client, credential, task_id, lease_token, [
        _discovery_row("4902", "GSH0001", ["2390000000000001"]),
        _discovery_row("4902", "GSH0002", [], environment_status="running",
                       status="running"),
    ])
    assert inconsistent.status_code == 422
    assert inconsistent.json()["detail"]["code"] == "executor_progress_snapshot_invalid"

    missing_status = _progress(device_client, credential, task_id, lease_token, [{
        **_discovery_row("4902", "GSH0001", ["2390000000000001"]),
        "environmentStatus": "",
    }])
    assert missing_status.status_code == 422
    assert missing_status.json()["detail"]["code"] == "executor_progress_snapshot_invalid"


def test_normal_scan_rejects_discovery_fields(context) -> None:
    """普通扫描行携带发现字段属于闭集外污染，进度整体被拒。"""
    web_client, device_client, ids, _database = context
    executor_id, credential = ids["executorId"], ids["credential"]
    heartbeat(device_client, credential, capabilities=DISCOVERY_CAPABILITIES,
              client_version=CLIENT_VERSION)
    created = web_client.post(
        "/v1/after-sale/scan",
        json={"idempotencyKey": "as-scan-pollute-00001",
              "executorId": executor_id, "browserMode": "headless",
              "concurrency": 2, "environmentSerials": ["4902"],
              "purpose": "claimable_orders"},
        headers=CSRF)
    assert created.status_code == 202, created.text
    task_id = created.json()["data"]["taskId"]
    task_id2, lease_token = _lease_and_start(device_client, credential)
    assert task_id2 == task_id
    dirty = _progress(device_client, credential, task_id, lease_token, [
        _discovery_row("4902", "GSH0001", ["2390000000000001"]),
    ])
    assert dirty.status_code == 422
    assert dirty.json()["detail"]["code"] == "executor_progress_snapshot_invalid"


def test_discovery_finish_requires_terminal_coverage(context) -> None:
    web_client, device_client, ids, _database = context
    executor_id, credential = ids["executorId"], ids["credential"]
    heartbeat(device_client, credential, capabilities=DISCOVERY_CAPABILITIES,
              client_version=CLIENT_VERSION)
    task_id = _create_task(web_client, executor_id, ["4902", "4901"],
                           key="as-discovery-cover-0001").json()["data"]["taskId"]
    task_id2, lease_token = _lease_and_start(device_client, credential)
    assert task_id2 == task_id

    def finish(summary):
        return device_client.post(
            f"/v1/executor-channel/tasks/{task_id}/finish",
            json={"leaseToken": lease_token, "outcome": "succeeded",
                  "resultCode": "after_sale_discovery_completed",
                  "resultSummary": summary},
            headers=device_headers(credential))

    # 只有 4902 出现：4901 未覆盖
    missing = finish({"rows": [
        _discovery_row("4902", "GSH0001", ["2390000000000001"])],
        "totalCount": 2, "claimableCount": 0, "refundCount": 1,
        "progressCompleted": 2, "progressTotal": 2})
    assert missing.status_code == 422
    assert missing.json()["detail"]["code"] == "executor_result_invalid"

    # 环境仍是 running：不得冒充全部查完
    not_terminal = finish({"rows": [
        _discovery_row("4902", "GSH0001", ["2390000000000001"]),
        _discovery_row("4901", "", [], environment_status="running",
                       status="running")],
        "totalCount": 2, "claimableCount": 0, "refundCount": 1,
        "progressCompleted": 2, "progressTotal": 2})
    assert not_terminal.status_code == 422
    assert not_terminal.json()["detail"]["code"] == "executor_result_invalid"

    # 可申请计数必须是 0：发现行不得携带申请语义
    claimable = finish({"rows": [
        _discovery_row("4902", "GSH0001", []),
        _discovery_row("4901", "", [], status="empty")],
        "totalCount": 2, "claimableCount": 1, "refundCount": 0,
        "progressCompleted": 2, "progressTotal": 2})
    assert claimable.status_code == 422

    # refundCount 与行内去重退款单数不一致
    mismatch = finish({"rows": [
        _discovery_row("4902", "GSH0001", ["2390000000000001"]),
        _discovery_row("4901", "", [], status="empty")],
        "totalCount": 2, "claimableCount": 0, "refundCount": 3,
        "progressCompleted": 2, "progressTotal": 2})
    assert mismatch.status_code == 422

    ok = finish({"rows": [
        _discovery_row("4902", "GSH0001", ["2390000000000001"]),
        _discovery_row("4901", "", [], status="empty")],
        "totalCount": 2, "claimableCount": 0, "refundCount": 1,
        "progressCompleted": 2, "progressTotal": 2})
    assert ok.status_code == 200, ok.text


def test_validate_discovered_items_excludes_conflicts_and_keeps_order(context) -> None:
    """发现身份与提交/跟踪历史核对：冲突排除并解释，不覆盖既有绑定。"""
    web_client, device_client, ids, database = context
    executor_id, credential = ids["executorId"], ids["credential"]
    with database.session_factory() as session:
        from xynigo_auth.models import LocalExecutor, User
        tenant = session.get(LocalExecutor, uuid.UUID(executor_id)).tenant_id
        user = session.scalar(select(User.id).where(User.tenant_id == tenant))
        run = AfterSaleClaimRun(tenant_id=tenant, actor_user_id=user,
            source_run_key=str(uuid.uuid4()), payload_hash='b' * 64,
            status='completed', total_count=1, success_count=1, failed_count=0,
            source='synthetic')
        session.add(run)
        session.flush()
        # 3000000000000002 已绑定到其他环境/订单（提交历史）
        session.add(AfterSaleClaimResult(run_id=run.id, tenant_id=tenant,
            environment_serial='8888', order_no='OTHER-ORDER', status='ok',
            refund_bill_id='3000000000000002', refunds=[], store_name='Synthetic'))
        # 3000000000000003 已绑定到其他环境/订单（跟踪历史）
        session.add(AfterSaleRefundTracking(tenant_id=tenant,
            environment_serial='7777', order_no='OTHER-ORDER-2',
            refund_bill_id='3000000000000003', phase='processing', last_status='ok'))
        # 其他租户的同号绑定不构成冲突
        other = Tenant(feishu_tenant_key='discovery-other-tenant')
        session.add(other)
        session.flush()
        session.add(AfterSaleRefundTracking(tenant_id=other.id,
            environment_serial='6666', order_no='FOREIGN',
            refund_bill_id='3000000000000001', phase='processing', last_status='ok'))
        session.commit()

    response = web_client.post("/v1/after-sale/track/validate-discovered", headers=CSRF,
        json={"items": [
            {"environmentSerial": "4902", "orderNo": "GSH0001",
             "refundBillId": "3000000000000001", "storeName": "合成环境-4902"},
            {"environmentSerial": "4902", "orderNo": "GSH0001",
             "refundBillId": "3000000000000002", "storeName": "合成环境-4902"},
            {"environmentSerial": "4901", "orderNo": "GSH0009",
             "refundBillId": "3000000000000003", "storeName": "合成环境-4901"},
        ]})
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["billCount"] == 1
    assert [item["refundBillId"] for item in data["items"]] == ["3000000000000001"]
    reasons = {item["refundBillId"]: item["reason"] for item in data["conflicts"]}
    assert set(reasons) == {"3000000000000002", "3000000000000003"}
    assert "提交历史" in reasons["3000000000000002"]
    assert "跟踪历史" in reasons["3000000000000003"]

    # 同一退款单在同一批次重复 → 契约层拒绝
    duplicate = web_client.post("/v1/after-sale/track/validate-discovered",
        headers=CSRF, json={"items": [
            {"environmentSerial": "4902", "orderNo": "GSH0001",
             "refundBillId": "3000000000000001"},
            {"environmentSerial": "4901", "orderNo": "GSH0002",
             "refundBillId": "3000000000000001"}]})
    assert duplicate.status_code == 422


def _row_model(**overrides):
    from xynigo_auth.operation_contract import AfterSaleScanRow
    base = _discovery_row("4902", "GSH0001", ["2390000000000001"])
    base.update(overrides)
    return AfterSaleScanRow.model_validate(base)


def test_discovery_row_diagnostic_reasons() -> None:
    """进度/结果共用的行级校验：逐条 diagnostic reason 不混。"""
    from xynigo_auth.executor_service import ExecutorChannelService, ExecutorServiceError

    payload = {"environmentSerials": ["4902", "4901"]}

    def reason(rows, *, terminal=False):
        try:
            ExecutorChannelService._validate_after_sale_discovery_rows(
                [_row_model(**row) if isinstance(row, dict) else row
                 for row in rows], payload, require_terminal_coverage=terminal)
        except ExecutorServiceError as exc:
            return exc.diagnostic_reason
        return None

    assert reason([{"environmentSerial": "4999"}]) == "environment_outside_task"
    assert reason([{}, {"orderNo": "GSH0002", "refundBillIds": []}]) is None  # 同环境同状态合法
    assert reason([{"claimable": True, "packageCount": 1}]) == (
        "discovery_claimable_semantics_invalid")
    assert reason([{"environmentStatus": "running", "status": "running"},
                   {"orderNo": "GSH0002"}]) == "environment_status_inconsistent"
    assert reason([{"environmentStatus": ""}]) == "environment_status_missing"
    assert reason([{"refundBillIds": ["2390000000000001"]},
                   {"orderNo": "GSH0002",
                    "refundBillIds": ["2390000000000001"]}]) == (
        "duplicate_refund_identity")
    # 终态覆盖：缺环境 / 非终态
    assert reason([{"environmentStatus": "ok"}], terminal=True) == (
        "environment_not_covered")
    assert reason([{"environmentStatus": "running", "status": "running"},
                   {"environmentSerial": "4901", "orderNo": "",
                    "refundBillIds": []}],
                  terminal=True) == "environment_not_terminal"
    # 空载荷（解密失败兜底）不能降为不校验范围
    try:
        ExecutorChannelService._validate_after_sale_discovery_rows(
            [_row_model()], {"environmentSerials": []},
            require_terminal_coverage=False)
    except ExecutorServiceError as exc:
        assert exc.diagnostic_reason == "request_payload_unavailable"
    else:
        raise AssertionError("empty request payload must be rejected")
