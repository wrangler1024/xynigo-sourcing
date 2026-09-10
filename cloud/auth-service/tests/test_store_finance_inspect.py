# -*- coding: utf-8 -*-
"""店铺结算巡检：云端幂等落库 / 快照 / 导出契约测试。"""
from __future__ import annotations

from io import BytesIO

from openpyxl import load_workbook
from test_purchase_api import authenticated_client

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


def test_store_finance_run_ingest_snapshot_and_export(tmp_path) -> None:
    client, database, headers = authenticated_client(tmp_path)

    # 幂等创建：直接经 service 层建 Run（发起路由需要在线执行器，单独覆盖）
    import uuid
    from sqlalchemy import select
    from xynigo_auth.models import (
        LocalExecutor,
        SessionRecord,
        StoreFinanceInspectRun,
        User,
    )
    from xynigo_auth.operation_contract import StoreFinanceRunCreateBody
    from xynigo_auth.operation_service import OperationRunService

    with database.session_factory() as session:
        # 用登录会话对应的租户/用户，保证后续 PUT 上报租户一致
        record = session.scalar(
            select(SessionRecord)
            .order_by(SessionRecord.created_at.desc())
            .limit(1)
        )
        user = session.get(User, record.user_id)
        from xynigo_auth.models import Tenant
        tenant = session.get(Tenant, user.tenant_id)
        executor = LocalExecutor(
            id=uuid.uuid4(),
            tenant_id=tenant.id,
            owner_user_id=user.id,
            display_name="测试执行器",
            platform="macos",
            architecture="arm64",
            client_version="0.17.18",
            credential_digest="test-digest-" + uuid.uuid4().hex[:24],
            status="active",
        )
        session.add(executor)
        session.flush()
        body = StoreFinanceRunCreateBody(
            idempotencyKey="store-finance-create-0001",
            executorId=executor.id,
            environmentSerials=["1746", "1775875785", "1775875509"],
        )
        runs = OperationRunService(session)
        run, unchanged = runs.create_store_finance_run(
            tenant_id=tenant.id,
            actor_user_id=user.id,
            body=body,
        )
        session.commit()
        run_id = run.id
        # 幂等重放：同 key 同内容返回既有 Run
        again, unchanged_again = runs.create_store_finance_run(
            tenant_id=tenant.id,
            actor_user_id=user.id,
            body=body,
        )
        assert unchanged_again is True
        assert again.id == run_id

    # 执行器完成上报（HTTP，无执行器凭证也可上报，归属执行器为空）
    payload = _run_body()
    first = client.put(
        "/v1/operations/store-finance-inspect-runs",
        json=payload,
        headers=headers,
    )
    assert first.status_code == 200, first.text
    data = first.json()["data"]
    assert data["status"] == "partial_failure"
    assert data["successCount"] == 2
    assert data["failedCount"] == 1
    assert len(data["rows"]) == 3

    # 重复上报完全相同内容：幂等，不产生第二份数据
    repeat = client.put(
        "/v1/operations/store-finance-inspect-runs",
        json=payload,
        headers=headers,
    )
    assert repeat.status_code == 200
    assert repeat.json()["data"]["rows"] == data["rows"]

    # 快照端点
    latest = client.get(
        "/v1/operation-runs/store-finance-inspect/latest",
        headers=headers,
    )
    assert latest.status_code == 200
    snapshot = latest.json()["data"]
    assert snapshot["runId"] == data["runId"]
    assert snapshot["rows"][0]["storeName"] == "山岚"
    assert snapshot["rows"][0]["inTransitAmount"] == 3031.29

    # 标准导出：六列表头 + 合计行（openpyxl 可读）
    export = client.get(
        "/v1/operation-runs/store-finance-inspect/"
        f"{data['runId']}/export?variant=standard",
        headers=headers,
    )
    assert export.status_code == 200
    values = _sheet_values(export.content)
    assert values[0][:2] == ("店铺中文名", "在途订单金额")
    names = [row[0] for row in values[1:-1]]
    assert set(names) >= {"山岚", "花间", "蓝天"}
    assert values[-1][0] == "合计"

    # 完整导出：CSV 含状态与异常说明列
    full = client.get(
        "/v1/operation-runs/store-finance-inspect/"
        f"{data['runId']}/export?variant=full",
        headers=headers,
    )
    assert full.status_code == 200
    text = full.content.decode("utf-8-sig")
    assert "异常说明" in text
    assert "二次验证" in text or "验证未通过" in text


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
    names = [row[0] for row in values[1:-1]]
    assert set(names) == {"花间", "山岚"}
    content2, filename2, _ = build_store_finance_export(
        rows, variant="full")
    assert filename2.endswith("_full.csv")
    assert "已完成结算收入" in content2.decode("utf-8-sig")
