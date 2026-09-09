from __future__ import annotations

import json
from urllib.parse import unquote

import httpx
import pytest
from xynigo_auth.procurement_import_sheet import (
    FeishuSheetsGateway,
    LarkSheetSyncError,
    parse_lark_sheet_url,
)


@pytest.mark.parametrize("apply_write", [True, False])
def test_full_purchase_urls_are_verified_after_server_auto_linking(apply_write: bool) -> None:
    first = "https://example.com/item?a=1&b=2#sku=blue%2FL"
    second = "https://example.com/second"
    cells: dict[int, object] = {
        2: [{"text": "打开采购链接", "type": "url", "link": first}],
        3: {"text": second, "type": "text"},
    }
    writes = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = unquote(request.url.path)
        if path.endswith("/tenant_access_token/internal"):
            return httpx.Response(200, json={
                "code": 0, "tenant_access_token": "synthetic-token", "expire": 7200,
            })
        if "/values/" in path:
            assert path.endswith("sheetA!M2:M3")
            return httpx.Response(200, json={"code": 0, "data": {
                "valueRange": {"values": [[cells[2]], [cells[3]]]},
            }})
        if path.endswith("/values_batch_update"):
            ranges = json.loads(request.content)["valueRanges"]
            writes.extend(ranges)
            assert ranges == [{
                "range": "sheetA!M2:M2",
                "values": [[{"text": first, "type": "text"}]],
            }]
            if apply_write:
                cells[2] = [{"text": first, "type": "url", "link": first,
                             "cellPosition": None}]
            return httpx.Response(200, json={"code": 0, "data": {"revision": 8}})
        raise AssertionError(f"unexpected request: {request.method} {path}")

    gateway = FeishuSheetsGateway(
        app_id="cli_test", app_secret="synthetic-secret",
        transport=httpx.MockTransport(handler),
    )
    url = "https://tenant.feishu.cn/sheets/SheetToken123"
    expected = {2: first, 3: second}
    assert gateway.hyperlink_presence(url, "sheetA", expected, column="M") == {
        2: False, 3: True,
    }
    gateway.set_hyperlinks(url, "sheetA", [(1, first), (2, first), (4, "")], column="M")
    assert len(writes) == 1
    # A successful response does not prove an abbreviated label was replaced.
    assert gateway.hyperlink_presence(url, "sheetA", expected, column="M") == {
        2: apply_write, 3: True,
    }


def test_cloud_sheet_gateway_uses_tenant_identity_and_official_endpoints() -> None:
    calls: list[tuple[str, str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        calls.append((request.method, unquote(request.url.path), body))
        if request.url.path.endswith("/tenant_access_token/internal"):
            return httpx.Response(
                200,
                json={"code": 0, "tenant_access_token": "tenant-token", "expire": 7200},
            )
        assert request.headers["authorization"] == "Bearer tenant-token"
        if request.url.path.endswith("/spreadsheets/SheetToken123"):
            return httpx.Response(200, json={"code":0,"data":{"spreadsheet":{"title":"合成采购协作表"}}})
        if request.url.path.endswith("/sheets/query"):
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "sheets": [
                            {
                                "sheet_id": "sheetA",
                                "title": "采购执行协作区",
                                "hidden": False,
                                "resource_type": "sheet",
                                "grid_properties": {
                                    "row_count": 200,
                                    "column_count": 43,
                                },
                            }
                        ]
                    },
                },
            )
        if "/values/" in request.url.path:
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "revision": 7,
                        "valueRange": {
                            "values": [
                                ["分单日期", "销售订单号"],
                                ["2026-08-26", "ORDER-1"],
                            ]
                        },
                    },
                },
            )
        if request.url.path.endswith("/values_image"):
            assert body["range"] == "sheetA!L2:L2"
            assert body["image"]
            assert body["name"].endswith(".jpg")
            return httpx.Response(200, json={"code": 0, "data": {"revision": 8}})
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    gateway = FeishuSheetsGateway(
        app_id="cli_test",
        app_secret="synthetic-secret",
        transport=httpx.MockTransport(handler),
    )
    url = "https://tenant.feishu.cn/sheets/SheetToken123"
    info = gateway.inspect(url)
    assert info["sheets"][0]["sheetId"] == "sheetA"
    table = gateway.read_table(url, "sheetA")
    assert table.headers == ("分单日期", "销售订单号")
    assert table.rows == ((2, ("2026-08-26", "ORDER-1")),)
    gateway.set_image(
        url, "sheetA", 2, b"\xff\xd8synthetic\xff\xd9", "image/jpeg", column="L"
    )
    assert gateway.verify_image(url, "sheetA", 2, column="L") is True
    assert (
        sum(
            path.endswith("tenant_access_token/internal")
            for _method, path, _body in calls
        )
        == 1
    )


def test_cloud_sheet_gateway_fails_closed_on_document_permission_and_bad_urls() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/tenant_access_token/internal"):
            return httpx.Response(
                200,
                json={"code": 0, "tenant_access_token": "tenant-token", "expire": 7200},
            )
        return httpx.Response(400, json={"code": 1310213, "msg": "permission fail"})

    gateway = FeishuSheetsGateway(
        app_id="cli_test",
        app_secret="synthetic-secret",
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(LarkSheetSyncError, match="文档应用"):
        gateway.inspect("https://tenant.feishu.cn/sheets/SheetToken123")
    with pytest.raises(LarkSheetSyncError, match="普通飞书电子表格"):
        parse_lark_sheet_url("https://tenant.feishu.cn/base/BaseToken123")
    with pytest.raises(LarkSheetSyncError, match="官方 HTTPS"):
        parse_lark_sheet_url("https://example.com/sheets/SheetToken123")


def test_cloud_sheet_gateway_retries_90204_only_for_idempotent_presentation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    style_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal style_calls
        body = json.loads(request.content) if request.content else None
        if request.url.path.endswith("/tenant_access_token/internal"):
            return httpx.Response(
                200,
                json={"code": 0, "tenant_access_token": "tenant-token", "expire": 7200},
            )
        if request.url.path.endswith("/styles_batch_update"):
            style_calls += 1
            if style_calls == 1:
                return httpx.Response(
                    400, json={"code": 90204, "msg": "WrongRequestBody"}
                )
            assert body["data"][0]["ranges"] == ["sheetA!A1:AQ1"]
            return httpx.Response(200, json={"code": 0, "data": {}})
        if request.url.path.endswith("/dimension_range"):
            assert body == {
                "dimension": {
                    "sheetId": "sheetA",
                    "majorDimension": "ROWS",
                    "startIndex": 1,
                    "endIndex": 1,
                },
                "dimensionProperties": {"visible": True, "fixedSize": 36},
            }
            return httpx.Response(200, json={"code": 0, "data": {}})
        if request.url.path.endswith("/sheets_batch_update"):
            properties = body["requests"][0]["updateSheet"]["properties"]
            assert properties["frozenRowCount"] == 1
            assert properties["frozenColCount"] == 5
            return httpx.Response(200, json={"code": 0, "data": {}})
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    monkeypatch.setattr(
        "xynigo_auth.procurement_import_sheet.time.sleep", lambda _value: None
    )
    gateway = FeishuSheetsGateway(
        app_id="cli_test",
        app_secret="synthetic-secret",
        transport=httpx.MockTransport(handler),
    )
    result = gateway.apply_header_presentation(
        "https://tenant.feishu.cn/sheets/SheetToken123",
        "sheetA",
        "采购执行协作区",
        [{"start": "A", "end": "AQ", "color": "#1B7280"}],
    )
    assert result == {"operations": 3}
    assert style_calls == 2


def test_cloud_sheet_gateway_never_blindly_retries_row_append_on_90204() -> None:
    append_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal append_calls
        if request.url.path.endswith("/tenant_access_token/internal"):
            return httpx.Response(
                200,
                json={"code": 0, "tenant_access_token": "tenant-token", "expire": 7200},
            )
        if request.url.path.endswith("/spreadsheets/SheetToken123"):
            return httpx.Response(200, json={"code":0,"data":{"spreadsheet":{"title":"合成采购协作表"}}})
        if request.url.path.endswith("/sheets/query"):
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "sheets": [
                            {
                                "sheet_id": "sheetA",
                                "title": "采购执行协作区",
                                "hidden": False,
                                "resource_type": "sheet",
                                "grid_properties": {
                                    "row_count": 200,
                                    "column_count": 43,
                                },
                            }
                        ]
                    },
                },
            )
        if "/values/" in request.url.path:
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {"valueRange": {"values": [["导入批次"]]}},
                },
            )
        if request.url.path.endswith("/values_append"):
            append_calls += 1
            return httpx.Response(400, json={"code": 90204, "msg": "WrongRequestBody"})
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    gateway = FeishuSheetsGateway(
        app_id="cli_test",
        app_secret="synthetic-secret",
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(LarkSheetSyncError, match="90204"):
        gateway.append_table_rows(
            "https://tenant.feishu.cn/sheets/SheetToken123",
            "采购执行协作区",
            ["导入批次"],
            [["batch_test"]],
        )
    assert append_calls == 1


def test_cloud_sheet_gateway_translates_excel_date_format_for_feishu_v2() -> None:
    style_body: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal style_body
        body = json.loads(request.content) if request.content else None
        if request.url.path.endswith("/tenant_access_token/internal"):
            return httpx.Response(
                200,
                json={"code": 0, "tenant_access_token": "tenant-token", "expire": 7200},
            )
        if request.url.path.endswith("/spreadsheets/SheetToken123"):
            return httpx.Response(200, json={"code":0,"data":{"spreadsheet":{"title":"合成采购协作表"}}})
        if request.url.path.endswith("/sheets/query"):
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "sheets": [
                            {
                                "sheet_id": "sheetA",
                                "title": "采购执行协作区",
                                "hidden": False,
                                "resource_type": "sheet",
                                "grid_properties": {
                                    "row_count": 200,
                                    "column_count": 43,
                                },
                            }
                        ]
                    },
                },
            )
        if request.url.path.endswith("/values_batch_update"):
            assert body["valueRanges"] == [
                {"range": "sheetA!A29:A29", "values": [[46260]]},
                {"range": "sheetA!A30:A30", "values": [[46260]]},
            ]
            return httpx.Response(200, json={"code": 0, "data": {}})
        if request.url.path.endswith("/styles_batch_update"):
            style_body = body
            return httpx.Response(200, json={"code": 0, "data": {}})
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    gateway = FeishuSheetsGateway(
        app_id="cli_test",
        app_secret="synthetic-secret",
        transport=httpx.MockTransport(handler),
    )
    result = gateway.normalize_date_column(
        "https://tenant.feishu.cn/sheets/SheetToken123",
        "采购执行协作区",
        ["分单日期"],
        [(29, ["2026-08-26"]), (30, ["2026-08-26"])],
        number_format="yyyy-mm-dd",
    )
    assert result == {"operations": 2, "rows": 2}
    assert style_body["data"][0]["style"]["formatter"] == "yyyy-MM-dd"
    assert style_body["data"][0]["ranges"] == ["sheetA!A29:A30"]


def test_curp_upgrade_keeps_existing_rows_and_manual_identifiers_untouched():
    from xynigo_auth.procurement_import_core import OUTPUT_HEADERS
    class Probe(FeishuSheetsGateway):
        def __init__(self): self.calls=[]
        def _insert_columns(self, url, sheet, index, count): self.calls.append(('insert', index, count))
        def _write_values(self, url, sheet, cell_range, values): self.calls.append(('write', cell_range, values))
    gateway=Probe()
    old=tuple(name for name in OUTPUT_HEADERS if name!='CURP')
    gateway.normalize_collaboration_headers('https://tenant.feishu.cn/sheets/SheetToken123','sheetA','采购分单协作区',old,rows=[(2,['']*len(old))])
    assert gateway.calls==[('insert',25,1),('write','Z1:Z1',[['CURP']])]
    gateway.calls.clear()
    current=['']*len(OUTPUT_HEADERS);current[25]='TESTCURP0000000001'
    gateway.normalize_collaboration_headers('https://tenant.feishu.cn/sheets/SheetToken123','sheetA','采购分单协作区',OUTPUT_HEADERS,rows=[(2,current)])
    assert gateway.calls==[]
    assert current[25]=='TESTCURP0000000001'
