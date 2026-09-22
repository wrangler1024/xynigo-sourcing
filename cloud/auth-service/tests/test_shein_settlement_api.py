# -*- coding: utf-8 -*-
"""结算看板接口：鉴权、序列化、空态（全部合成数据，不打真实网关）。

对接层最常出的两类问题在这里钉住：
- 权限漏了 → 没权限的人也能触发同步（会打真实平台、消耗配额）
- 金额序列化成 JSON number → JS 侧浮点误差；这里统一两位小数字符串
"""
from __future__ import annotations

import json
import uuid
from datetime import date, datetime, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, select

from xynigo_auth.config import Settings
from xynigo_auth.database import Database
from xynigo_auth.main import ADMIN_ROLE, _ensure_system_catalog, create_app, utcnow
from xynigo_auth.models import (
    Base,
    Permission,
    Role,
    RolePermission,
    SessionRecord,
    SheinAuthorizedStore,
    Tenant,
    User,
    UserRole,
)
from xynigo_auth.security import hash_token
from xynigo_auth.shein_settlement_sync import FxRateQuote
from xynigo_auth.shein_store_auth_crypto import SheinStoreSecretCipher

ADMIN_TOKEN = "admin-token-000000000000"
MEMBER_TOKEN = "member-token-0000000000"
FERNET_KEY = "MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA="
SECRET_PLAIN = "C" * 32


# 「下次结算」按真实时钟动态判定（pay_date>=今天）：打款日必须用运行时
# 计算的未来日期,写死日历日会在次日起整组变红（20260922 复现过）。
from datetime import date as _date, timedelta as _timedelta
FUTURE_PAY_DATE = (_date.today() + _timedelta(days=7)).isoformat()
FUTURE_PAY_TIME = FUTURE_PAY_DATE + " 10:00:00"


def shein_transport(*, fail: bool = False):
    """最小假网关：够同步跑一轮（订单/详情/对账单/报账单/站点币种）。"""

    def handler(request: httpx.Request) -> httpx.Response:
        if fail:
            return httpx.Response(
                200, json={"code": "openapi00001",
                           "msg": "签名错误:生成的签名不正确"})
        path = request.url.path
        if path == "/open-api/order/order-list":
            return httpx.Response(200, json={
                "code": "0", "msg": "OK", "info": {"count": 1, "orderList": [
                    {"orderNo": "O1", "orderStatus": 4,
                     "orderCreateTime": "2026-09-10 10:00:00",
                     "orderUpdateTime": "2026-09-16 10:00:00"}]}})
        if path == "/open-api/order/order-detail":
            return httpx.Response(200, json={
                "code": "0", "msg": "OK", "info": {"orderList": [
                    {"orderNo": "O1", "estimatedGrossIncome": 88.8,
                     "currencyCode": "MXN"}]}})
        if path == "/open-api/finance/get-check-order-list":
            # 必须按 startAddTime/endAddTime（对账单**生成时间**）过滤：
            # 生产按生成时间分片，每张对账单只落一个窗口。假网关若忽略窗口，
            # 同一批数据会被每个窗口各算一次（实测把 500.25 算成了 2001.00）。
            payload = json.loads(request.content or b"{}")
            start = str(payload.get("startAddTime") or "")
            end = str(payload.get("endAddTime") or "")
            add_time = "2026-09-15 10:00:00"
            items = ([{"checkOrderNo": "B-s1", "addTime": add_time,
                       "estimatePayTime": FUTURE_PAY_TIME,
                       "currencyCode": "MXN",
                       "estimateIncomeMoneyTotal": 500.25,
                       "incomeExpenditureType": 1}]
                     if start <= add_time < end else [])
            return httpx.Response(200, json={
                "code": "0", "msg": "OK", "info": {"count": len(items),
                                                   "list": items}})
        if path == "/open-api/finance/report-order-list":
            return httpx.Response(200, json={
                "code": "0", "msg": "OK", "info": {"count": 1, "list": [
                    {"income": 1200.5, "currencyCode": "MXN",
                     "reportStatus": 2, "paymentMethod": 2,
                     "rxAcct": "acct-x"}]}})
        if path == "/open-api/goods/query-site-list":
            return httpx.Response(200, json={
                "code": "0", "msg": "OK",
                "info": {"data": [{"sub_site_list": [{"currency": "MXN"}]}]}})
        return httpx.Response(404, json={"code": "404", "msg": "not found"})

    return httpx.MockTransport(handler)


def build_app(tmp_path, *, transport=None, settlement_sync_enabled=False):
    database_url = f"sqlite+pysqlite:///{tmp_path / 'settle.sqlite3'}"
    database = Database(database_url)

    # SQLite 是整文件写锁：同步会在请求内做较多写入，而请求日志中间件也要写库，
    # 默认 journal 模式下两者互锁。生产用 Postgres 无此问题，这里开 WAL +
    # 忙等超时让测试环境能反映真实行为。
    @event.listens_for(database.engine, "connect")
    def _sqlite_pragmas(dbapi_connection, _record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=10000")
        cursor.close()

    Base.metadata.create_all(database.engine)
    settings = Settings(
        environment="test",
        database_url=database_url,
        feishu_app_id="cli_test",
        feishu_app_secret="test-secret-not-real",
        buyer_credential_encryption_key=FERNET_KEY,
        feishu_redirect_uri="http://testserver/v1/auth/feishu/callback",
        allowed_tenant_keys="tenant_allowed",
        cookie_secure=False,
        allowed_hosts="testserver",
        shein_openapi_app_id="APPID0001",
        shein_openapi_app_secret="app-secret-not-real",
        shein_auth_redirect_base="http://testserver",
        settlement_sync_enabled=settlement_sync_enabled,
    )
    def _stub_fx_fetcher() -> FxRateQuote:
        # 确定性 stub：空汇率 = 汇率源不可用，cny 折算为 None。
        # 汇率接线的详细行为在 test_shein_settlement_sync 里覆盖。
        return FxRateQuote(rates={}, rate_date=date.today())

    app = create_app(
        settings=settings, oauth_client=object(), directory_client=object(),
        database=database,
        shein_openapi_transport=transport or shein_transport(),
        settlement_fx_fetcher=_stub_fx_fetcher,
    )
    now = utcnow()
    with database.session_factory() as session:
        tenant = Tenant(feishu_tenant_key="tenant_allowed", name="合成组织")
        session.add(tenant)
        session.flush()
        admin = User(tenant_id=tenant.id, feishu_open_id="ou_admin",
                     display_name="合成管理员", status="active")
        member = User(tenant_id=tenant.id, feishu_open_id="ou_member",
                      display_name="无权限成员", status="active")
        session.add_all((admin, member))
        session.flush()
        # 权限目录是懒播种的：先触发一次，再把管理员挂到 admin 角色上
        _ensure_system_catalog(session, tenant=tenant)
        session.flush()
        admin_role = session.scalars(
            select(Role).where(Role.tenant_id == tenant.id,
                               Role.code == ADMIN_ROLE)
        ).one()
        finance = session.scalars(
            select(Permission).where(Permission.code == "finance.access")
        ).first()
        assert finance is not None, "finance.access 权限点缺失"
        # 管理员拿全部权限；成员挂一个空角色，用于验证 403
        empty_role = Role(tenant_id=tenant.id, code="empty", name="空角色",
                          is_system=True)
        session.add(empty_role)
        session.flush()
        session.add(UserRole(user_id=admin.id, role_id=admin_role.id))
        session.add(UserRole(user_id=member.id, role_id=empty_role.id))
        session.add_all((
            SessionRecord(user_id=admin.id, token_hash=hash_token(ADMIN_TOKEN),
                          last_seen_at=now, expires_at=now + timedelta(hours=8)),
            SessionRecord(user_id=member.id, token_hash=hash_token(MEMBER_TOKEN),
                          last_seen_at=now, expires_at=now + timedelta(hours=8)),
        ))
        session.commit()
        store = SheinAuthorizedStore(
            tenant_id=tenant.id, merchant_id="18301880", store_name="合成店铺",
            open_key_id="A" * 32,
            secret_ciphertext=SheinStoreSecretCipher(FERNET_KEY)
            .encrypt_secret(SECRET_PLAIN),
            app_id="APPID0001", mode="self",
            first_authorized_at=now, latest_authorized_at=now,
            status="ok", store_info={},
        )
        session.add(store)
        session.commit()
    return app, database


def admin_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {ADMIN_TOKEN}"}


def member_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {MEMBER_TOKEN}"}


# ---- 鉴权 ----

def test_summary_requires_finance_permission(tmp_path):
    app, _ = build_app(tmp_path)
    with TestClient(app) as client:
        assert client.get("/v1/finance/settlement/summary").status_code in (401, 403)
        assert client.get("/v1/finance/settlement/summary",
                          headers=member_headers()).status_code == 403
        assert client.get("/v1/finance/settlement/summary",
                          headers=admin_headers()).status_code == 200


def test_sync_requires_finance_permission(tmp_path):
    """同步会打真实平台并消耗配额，不能给没有财务权限的人调用。"""
    app, _ = build_app(tmp_path)
    with TestClient(app) as client:
        assert client.post("/v1/finance/settlement/sync",
                           headers=member_headers()).status_code == 403
        assert client.post("/v1/finance/settlement/sync",
                           headers=admin_headers()).status_code == 200


# ---- 空态：还没同步过 ----

def test_summary_before_any_sync_reports_not_synced(tmp_path):
    app, _ = build_app(tmp_path)
    with TestClient(app) as client:
        body = client.get("/v1/finance/settlement/summary",
                          headers=admin_headers()).json()
        assert body["storeTotal"] == 1
        assert body["storeOk"] == 0
        assert body["storeFailed"] == 1
        assert body["stores"][0]["error"] == "尚未同步"
        # 金额是 null 而不是 "0.00"——"没同步"和"确实是 0"必须分得开
        assert body["cards"]["settled_cumulative"]["groups"] == []
        assert body["cards"]["settled_cumulative"]["cnyTotal"] is None


# ---- 同步 + 读取闭环 ----

def test_sync_then_summary_roundtrip(tmp_path):
    app, _ = build_app(tmp_path)
    with TestClient(app) as client:
        synced = client.post("/v1/finance/settlement/sync",
                             headers=admin_headers()).json()
        assert synced["status"] == "succeeded"
        assert synced["storeOk"] == 1 and synced["storeFailed"] == 0
        assert synced["stores"][0]["status"] == "ok"
        uuid.UUID(synced["runId"])  # 必须是合法 runId

        body = client.get("/v1/finance/settlement/summary",
                          headers=admin_headers()).json()
        assert body["storeOk"] == 1
        store = body["stores"][0]
        assert store["storeName"] == "合成店铺"
        assert store["currency"] == "MXN"
        # 收款方式随快照留存但本期不呈现（真机确认首店为 2=钱包充值）
        assert store["paymentMethod"] == 2
        assert store["inTransitAmount"] == "88.80"
        assert store["nearestPayoutAmount"] == "500.25"
        assert store["payDates"] == [FUTURE_PAY_DATE]
        assert store["settledCumulativeAmount"] == "1200.50"

        card = body["cards"]["settled_cumulative"]
        assert card["groups"] == [
            {"currency": "MXN", "total": "1200.50", "cny": None}]
        assert card["cnyTotal"] is None       # 未配置汇率 → 显示「—」
        assert body["cards"]["nearest_payout"]["nearestPayDate"] == FUTURE_PAY_DATE
        assert body["schedule"][0]["payDate"] == FUTURE_PAY_DATE


def test_amounts_serialize_as_two_decimal_strings(tmp_path):
    """金额必须是字符串：JSON number 会在 JS 侧引入浮点误差。"""
    app, _ = build_app(tmp_path)
    with TestClient(app) as client:
        client.post("/v1/finance/settlement/sync", headers=admin_headers())
        raw = client.get("/v1/finance/settlement/summary",
                         headers=admin_headers()).text
        body = json.loads(raw)
    for card in body["cards"].values():
        for group in card["groups"]:
            assert isinstance(group["total"], str)
            assert len(group["total"].split(".")[-1]) == 2
    assert isinstance(body["stores"][0]["inTransitAmount"], str)


def test_sync_failure_is_reported_not_raised(tmp_path):
    """网关整体故障时同步接口仍返回 200 并说明失败——不能把异常抛给前端。"""
    app, _ = build_app(tmp_path, transport=shein_transport(fail=True))
    with TestClient(app) as client:
        body = client.post("/v1/finance/settlement/sync",
                           headers=admin_headers()).json()
        assert body["status"] == "failed"
        assert body["storeFailed"] == 1
        assert "签名" in body["stores"][0]["error"]

        summary = client.get("/v1/finance/settlement/summary",
                             headers=admin_headers()).json()
        assert summary["storeFailed"] == 1
        assert [a["kind"] for a in summary["alerts"]] == ["sync_fail"]


def test_repeated_sync_is_idempotent_at_api_level(tmp_path):
    app, database = build_app(tmp_path)
    with TestClient(app) as client:
        client.post("/v1/finance/settlement/sync", headers=admin_headers())
        client.post("/v1/finance/settlement/sync", headers=admin_headers())
        body = client.get("/v1/finance/settlement/summary",
                          headers=admin_headers()).json()
    # 重复同步不应把金额翻倍
    assert body["stores"][0]["inTransitAmount"] == "88.80"
    assert body["stores"][0]["nearestPayoutAmount"] == "500.25"


def test_concurrent_sync_returns_409(tmp_path):
    """已有同步在跑时，再点一次刷新应返回 409 而不是两边同时打平台。

    手动刷新与定时任务共用一个闸门——这是"同店不并发取数"的落点。
    """
    from sqlalchemy import select as _select

    from xynigo_auth.main import utcnow as _utcnow
    from xynigo_auth.models import SheinSettlementSyncRun

    app, database = build_app(tmp_path)
    with TestClient(app) as client:
        assert client.post("/v1/finance/settlement/sync",
                           headers=admin_headers()).status_code == 200
        # 人为把刚那轮改回 running，模拟"正在跑"
        with database.session_factory() as session:
            run = session.execute(_select(SheinSettlementSyncRun)).scalars().one()
            run.status = "running"
            run.finished_at = None
            session.commit()
        again = client.post("/v1/finance/settlement/sync",
                            headers=admin_headers())
        assert again.status_code == 409
        assert again.json()["detail"]["code"] == "shein_settlement_sync_busy"


def test_export_returns_xlsx_attachment(tmp_path):
    """导出走服务端生成 xlsx：金额是可求和的数值单元格，不是 CSV。"""
    import io

    from openpyxl import load_workbook

    app, _ = build_app(tmp_path)
    with TestClient(app) as client:
        client.post("/v1/finance/settlement/sync", headers=admin_headers())
        res = client.get("/v1/finance/settlement/export", headers=admin_headers())
        assert res.status_code == 200
        assert res.headers["content-type"].startswith(
            "application/vnd.openxmlformats")
        assert "attachment" in res.headers["content-disposition"]
        assert res.headers["x-xynigo-row-count"] == "1"
        sheet = load_workbook(io.BytesIO(res.content)).active
        assert sheet.title == "结算看板"
        assert sheet[1][0].value == "店铺"
        assert sheet[2][0].value == "合成店铺"
        assert sheet[2][3].value == 88.8          # 数值单元格，可求和
        assert sheet[2][3].number_format == "#,##0.00"


def test_export_requires_finance_permission(tmp_path):
    app, _ = build_app(tmp_path)
    with TestClient(app) as client:
        assert client.get("/v1/finance/settlement/export",
                          headers=member_headers()).status_code == 403


def test_export_respects_currency_filter(tmp_path):
    app, _ = build_app(tmp_path)
    with TestClient(app) as client:
        client.post("/v1/finance/settlement/sync", headers=admin_headers())
        res = client.get("/v1/finance/settlement/export?currency=USD",
                         headers=admin_headers())
        assert res.status_code == 200
        assert res.headers["x-xynigo-row-count"] == "0"   # 合成店铺是 MXN


def test_worker_is_installed_when_enabled(tmp_path):
    """评审必须改 #5 的回归位：开关打开时必须真的装上 worker。

    原实现把 worker 构造在 `settlement_sync_worker = None` **之前**，构造完立刻被
    置空——`SETTLEMENT_SYNC_ENABLED=true` 也不会启动线程，"到期才跑/重启不多打
    平台"只在单测里成立。
    """
    app, _ = build_app(tmp_path, settlement_sync_enabled=True)
    assert app.state.settlement_sync_worker is not None
    with TestClient(app):          # 触发 lifespan，确认能正常起停
        pass


def test_worker_absent_when_disabled(tmp_path):
    """默认关闭：部署即对外打网关是要避免的。"""
    app, _ = build_app(tmp_path)
    assert app.state.settlement_sync_worker is None
