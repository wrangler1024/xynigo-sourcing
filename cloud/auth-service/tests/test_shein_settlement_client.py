# -*- coding: utf-8 -*-
"""财务/订单域客户端：响应封装、签名、参数与批量上限（全部合成响应）。

最要紧的一条是**响应封装**：网关对店铺信息走 `data`、对财务与订单域走
`info`（开放平台文档示例 {"code":"0","msg":"OK","info":{…},"bbl":{},"traceId":…}）。
写错这一段的表现是每个财务接口都在运行时抛"返回数据缺失"，所以两条路径
都要有用例钉住，而不是只测新加的那条。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json

import httpx
import pytest

from xynigo_auth.shein_openapi_client import (
    CHECK_ORDER_DETAIL_PATH,
    CHECK_ORDER_LIST_PATH,
    ORDER_DETAIL_PATH,
    ORDER_DETAIL_MAX_BATCH,
    ORDER_LIST_PATH,
    REPORT_ORDER_LIST_PATH,
    SITE_LIST_PATH,
    SheinOpenApiClient,
    SheinOpenApiClientError,
)

OPEN_KEY_ID = "A" * 32
SECRET = "B" * 32
GATEWAY = "https://openapi.example.test"


def _client(handler) -> SheinOpenApiClient:
    return SheinOpenApiClient(
        gateway=GATEWAY, app_id="APPID0001", app_secret="APP_SECRET_1234",
        transport=httpx.MockTransport(handler),
    )


def _ok_info(info: dict) -> httpx.Response:
    return httpx.Response(
        200, json={"code": "0", "msg": "OK", "info": info, "bbl": {},
                   "traceId": "t-1"})


def test_signature_matches_documented_algorithm():
    """VALUE=openKeyId&timestamp&path；KEY=secret+RandomKey；HMAC-SHA256→hex→base64；
    signature=RandomKey+base64(hex)。签名错的表现是全接口 401。"""
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update({
            "openKeyId": request.headers["x-lt-openKeyId"],
            "timestamp": request.headers["x-lt-timestamp"],
            "signature": request.headers["x-lt-signature"],
        })
        return _ok_info({"count": 0, "list": []})

    _client(handler).query_report_orders(
        open_key_id=OPEN_KEY_ID, secret_key=SECRET)
    signature = seen["signature"]
    random_key, digest_b64 = signature[:5], signature[5:]
    # 平台签名规则：RandomKey 恰好 5 位（长度不对会被平台切错前缀 → 00001）
    assert len(random_key) == 5
    expected = hmac.new(
        (SECRET + random_key).encode(),
        f"{OPEN_KEY_ID}&{seen['timestamp']}&{REPORT_ORDER_LIST_PATH}".encode(),
        hashlib.sha256,
    ).hexdigest()
    assert digest_b64 == base64.b64encode(expected.encode()).decode()
    assert seen["timestamp"].isdigit() and len(seen["timestamp"]) == 13


def test_finance_endpoints_unwrap_info_envelope():
    def handler(request: httpx.Request) -> httpx.Response:
        return _ok_info({"count": 2, "list": [{"reportOrderNo": "R1"}]})

    data = _client(handler).query_report_orders(
        open_key_id=OPEN_KEY_ID, secret_key=SECRET)
    assert data["count"] == 2
    assert data["list"][0]["reportOrderNo"] == "R1"


def test_store_info_still_unwraps_data_envelope():
    """回归位：改封装逻辑不能把已生产验证的店铺信息接口弄坏。"""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "code": "0", "msg": "ok",
            "data": {"merchantId": "1", "storeName": "s"},
        })

    data = _client(handler).query_store_info(
        open_key_id=OPEN_KEY_ID, secret_key=SECRET)
    assert data["storeName"] == "s"


def test_missing_envelope_error_names_actual_keys():
    """两段都没有时要把真实顶层键带进错误信息，联调首日一眼看出封装变了。"""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": "0", "msg": "OK", "result": {}})

    with pytest.raises(SheinOpenApiClientError) as excinfo:
        _client(handler).query_report_orders(
            open_key_id=OPEN_KEY_ID, secret_key=SECRET)
    assert excinfo.value.code == "shein_gateway_data_invalid"
    assert "result" in excinfo.value.message


def test_business_error_code_propagates():
    """对账单窗口越界报 gsfs99401——同步层要靠 code 判断是否分片重试。"""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "code": "gsfs99401", "msg": "查询时间周期不可超过7天", "info": None})

    with pytest.raises(SheinOpenApiClientError) as excinfo:
        _client(handler).query_check_orders(
            open_key_id=OPEN_KEY_ID, secret_key=SECRET,
            start_add_time="2026-09-01 00:00:00",
            end_add_time="2026-09-20 00:00:00",
        )
    assert excinfo.value.code == "gsfs99401"


def test_check_order_list_sends_window_and_optional_filters():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return _ok_info({"count": 0, "list": []})

    _client(handler).query_check_orders(
        open_key_id=OPEN_KEY_ID, secret_key=SECRET,
        start_add_time="2026-09-14 00:00:00",
        end_add_time="2026-09-20 23:59:59",
        page=2, page_size=30, check_status=1,
    )
    assert captured == {
        "page": 2, "pageSize": 30,
        "startAddTime": "2026-09-14 00:00:00",
        "endAddTime": "2026-09-20 23:59:59",
        "checkStatus": 1,
    }


def test_check_order_detail_is_get_with_query_param():
    """对账单详情是 GET，单号走 query——写成 POST+body 会 404/签名不匹配。"""
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["path"] = request.url.path
        captured["query"] = dict(request.url.params)
        captured["content"] = request.content
        return _ok_info({"checkOrderNo": "B1", "itemList": []})

    data = _client(handler).get_check_order_detail(
        open_key_id=OPEN_KEY_ID, secret_key=SECRET, check_order_no="B1")
    assert captured["method"] == "GET"
    assert captured["path"] == CHECK_ORDER_DETAIL_PATH
    assert captured["query"] == {"checkOrderNo": "B1"}
    assert captured["content"] == b""
    assert data["checkOrderNo"] == "B1"


def test_order_details_batch_capped_at_documented_limit():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return _ok_info({"list": []})

    _client(handler).query_order_details(
        open_key_id=OPEN_KEY_ID, secret_key=SECRET,
        order_nos=[f"O{i}" for i in range(50)],
    )
    assert len(captured["orderNoList"]) == ORDER_DETAIL_MAX_BATCH
    assert captured["orderNoList"][0] == "O0"


def test_order_list_sends_query_type_and_status():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return _ok_info({"count": 0, "list": []})

    _client(handler).query_orders(
        open_key_id=OPEN_KEY_ID, secret_key=SECRET,
        query_type=2, start_time="2026-09-17 00:00:00",
        end_time="2026-09-18 00:00:00", order_status=4, page=1, page_size=30,
    )
    assert captured == {
        "queryType": 2, "startTime": "2026-09-17 00:00:00",
        "endTime": "2026-09-18 00:00:00", "orderStatus": 4,
        "page": 1, "pageSize": 30,
    }
    assert ORDER_LIST_PATH == "/open-api/order/order-list"


def test_site_list_is_post_with_empty_body():
    """实测：按 GET 调会报 openapi00007 请求头参数异常，正确方法是 POST。"""
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["content"] = request.content
        return _ok_info({"data": []})

    _client(handler).query_site_list(
        open_key_id=OPEN_KEY_ID, secret_key=SECRET)
    assert captured["method"] == "POST"
    assert captured["content"] == b"{}"
    assert SITE_LIST_PATH == "/open-api/goods/query-site-list"


def test_http_500_raises_gateway_http_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    with pytest.raises(SheinOpenApiClientError) as excinfo:
        _client(handler).query_report_orders(
            open_key_id=OPEN_KEY_ID, secret_key=SECRET)
    assert excinfo.value.code == "shein_gateway_http_error"
