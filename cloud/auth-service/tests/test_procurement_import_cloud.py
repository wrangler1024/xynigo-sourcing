from __future__ import annotations

import base64
import json
import threading
import time
from io import BytesIO
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from openpyxl import Workbook, load_workbook
from sqlalchemy import select
from test_auth_flow import build_test_app, start_login

from xynigo_auth.models import AuditEvent, ProcurementImportJob, ProcurementImportPlan
from xynigo_auth.procurement_import_core import OUTPUT_HEADERS, CollaborationSheetTarget
from xynigo_auth.procurement_import_sheet import SheetTable


class FakeCloudSheetGateway:
    def __init__(self) -> None:
        self.headers = tuple(OUTPUT_HEADERS)
        self.rows: tuple[tuple[int, tuple[object, ...]], ...] = ()
        self.backgrounds: dict[int, str] = {}
        self.links: dict[int, str] = {}
        self.lock = threading.Lock()

    def inspect(self, url):
        return {
            "url": "https://tenant.feishu.cn/sheets/SheetToken123",
            "spreadsheetToken": "SheetToken123",
            "revision": 1,
            "sheets": [
                {
                    "sheetId": "sheetA",
                    "sheetName": "采购执行协作区",
                    "rowCount": 200,
                    "columnCount": len(self.headers),
                    "hidden": False,
                }
            ],
        }

    def read_table(self, url, sheet_id):
        with self.lock:
            return SheetTable(self.headers, self.rows, revision=1)

    def append_table_rows(self, url, sheet_name, columns, rows, dtypes=None, formats=None):
        with self.lock:
            next_row = max([number for number, _values in self.rows] or [1]) + 1
            current = list(self.rows)
            current.extend(
                (next_row + index, tuple(row)) for index, row in enumerate(rows)
            )
            self.rows = tuple(current)
        return {"updated_rows_count": len(rows)}

    def normalize_collaboration_headers(self, *args, **kwargs):
        return {"operations": 0, "skipped": True}

    def reorder_collaboration_headers(self, *args, **kwargs):
        return {"operations": 0, "skipped": True}

    def normalize_date_column(self, *args, **kwargs):
        return {"operations": 0, "skipped": True}

    def apply_header_presentation(self, *args, **kwargs):
        return {"operations": 1}

    def row_backgrounds(self, url, sheet_id, row_numbers):
        return {int(row): self.backgrounds.get(int(row), "") for row in row_numbers}

    def apply_row_presentation(
        self, url, sheet_name, background_bands, row_ranges, row_height=52, last_column="AQ"
    ):
        for item in background_bands:
            for row in range(int(item["start"]), int(item["end"]) + 1):
                self.backgrounds[row] = str(item["color"])
        return {"operations": 1}

    def hyperlink_presence(self, url, sheet_id, expected_links, column="M"):
        return {
            int(row): self.links.get(int(row)) == link
            for row, link in dict(expected_links).items()
        }

    def set_hyperlinks(self, url, sheet_id, links, column="M"):
        for row, link in links:
            self.links[int(row)] = str(link)
        return {"operations": 1}

    def image_presence(self, url, sheet_id, row_numbers, column="L"):
        # Synthetic cloud fixture has no embedded image bytes. Pretend the
        # target already contains them so this test stays entirely offline.
        return {int(row): True for row in row_numbers}

    def set_image(self, *args, **kwargs):
        raise AssertionError("existing synthetic images must be skipped")

    def verify_image(self, *args, **kwargs):
        return True


def source_workbook() -> bytes:
    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = "order_"
    worksheet.append(
        [
            "店铺账号",
            "订单号",
            "包裹号",
            "下单时间",
            "订单金额",
            "币种缩写",
            "收货人姓名",
            "收货人国家",
            "收货人州/省",
            "收货人城市",
            "地址1",
            "地址2",
            "邮编",
            "收货人电话",
            "SKU",
            "产品规格",
            "产品售价",
            "单个产品数量",
            "产品图片网址",
            "客服备注",
            "产品图片",
        ]
    )
    xyp2 = {
        "d": "mx",
        "c": "MXN",
        "i": [
            [
                "SOURCE-01",
                "422790137",
                "I8mmn32aip2g7d",
                "27_447",
                "Multicolor",
                "M",
                110.09,
                0.65,
                38.53,
                1,
            ]
        ],
    }
    worksheet.append(
        [
            "测试店铺-测试运营（二组）$",
            "GSH-CLOUD-TEST-001",
            "PKG-CLOUD-TEST-001",
            "2026-08-26 12:00:00",
            150,
            "MXN",
            "Recipient Test",
            "MEXICO",
            "State",
            "City",
            "Address 1",
            "Address 2",
            "00123",
            "0012345678",
            "ERP-SKU-01",
            "SOURCE-01:Multicolor-M",
            150,
            1,
            "https://img.ltwebstatic.com/test/source-one.jpg",
            "[XYP2]" + json.dumps(xyp2, separators=(",", ":")) + "[/XYP2]",
            "",
        ]
    )
    output = BytesIO()
    workbook.save(output)
    return output.getvalue()


def login(client: TestClient) -> None:
    state, _challenge = start_login(client)
    response = client.get(
        "/v1/auth/feishu/callback",
        params={"code": "authorization-code", "state": state},
        follow_redirects=False,
    )
    assert response.status_code == 303


def test_cloud_parse_shared_source_sku_preserves_variant_amounts(tmp_path) -> None:
    workbook = load_workbook(BytesIO(source_workbook()))
    worksheet = workbook.active
    values = [cell.value for cell in worksheet[2]]
    payload = json.loads(values[19].split('[XYP2]')[1].split('[/XYP2]')[0])
    black = list(payload['i'][0])
    black[0], black[4], black[5] = 'SHARED-SOURCE', 'Negro', 'L'
    gray = list(black)
    gray[1:3], gray[4] = ['422790138', 'TEST-GRAY-SKU'], 'Gris'
    payload['i'] = [black, gray]
    remark = '[XYP2]' + json.dumps(payload) + '[/XYP2]'
    worksheet.delete_rows(2)
    # Deliberately reverse exported row order relative to the XYP2 details.
    for spec, amount in [('Gray-L', 220), ('Black-L', 110)]:
        row = list(values)
        row[4], row[15], row[16], row[19] = 330, 'SHARED-SOURCE:' + spec, amount, remark
        worksheet.append(row)
    stream = BytesIO()
    workbook.save(stream)
    gateway = FakeCloudSheetGateway()
    app, _database, _oauth = build_test_app(
        tmp_path, procurement_import_enabled=True,
        procurement_import_gateway=gateway,
    )
    with TestClient(app) as client:
        login(client)
        response = client.post(
            '/v1/assistant/procurement-import/parse',
            headers={'X-Xynigo-Web-CSRF': 'same-origin'},
            json={'filename': 'shared_sku_cloud.xlsx',
                  'contentBase64': base64.b64encode(stream.getvalue()).decode('ascii')},
        )
        assert response.status_code == 201, response.text
        plan = response.json()
        assert plan['runtime'] == 'cloud'
        assert plan['sourceRows'] == 2
        assert plan['orderCount'] == 1
        assert plan['detailCount'] == 2
        assert plan['issues'] == []
        assert [(row['mainSpec'], row['secondarySpec'], row['itemSalesAmount'])
                for row in plan['preview']] == [('Negro', 'L', 110), ('Gris', 'L', 220)]
        assert gateway.rows == ()


def test_cloud_parse_validate_export_and_durable_worker(tmp_path) -> None:
    gateway = FakeCloudSheetGateway()
    app, database, _oauth = build_test_app(
        tmp_path,
        procurement_import_enabled=True,
        procurement_import_gateway=gateway,
    )
    headers = {"X-Xynigo-Web-CSRF": "same-origin"}
    with TestClient(app) as client:
        login(client)
        parsed = client.post(
            "/v1/assistant/procurement-import/parse",
            headers=headers,
            json={
                "filename": "order_cloud_test.xlsx",
                "contentBase64": base64.b64encode(source_workbook()).decode("ascii"),
            },
        )
        assert parsed.status_code == 201, parsed.text
        plan = parsed.json()
        assert plan["runtime"] == "cloud"
        assert plan["orderCount"] == 1
        assert plan["detailCount"] == 1
        assert plan["quantityCount"] == 1
        assert plan["preview"][0]["orderNo"] == "GSH-CLOUD-TEST-001"
        assert plan["preview"][0]["store"] == "测试店铺-测试运营（二组）$"

        with database.session_factory() as session:
            stored = session.scalar(select(ProcurementImportPlan))
            assert stored is not None
            assert stored.status == "parsed"
            assert b"GSH-CLOUD-TEST-001" not in bytes(stored.encrypted_payload or b"")

        inspected = client.post(
            "/v1/assistant/procurement-import/target/inspect",
            headers=headers,
            json={
                "planId": plan["planId"],
                "spreadsheetUrl": "https://tenant.feishu.cn/sheets/SheetToken123",
            },
        )
        assert inspected.status_code == 200, inspected.text
        assert inspected.json()["sheets"][0]["sheetId"] == "sheetA"

        validated = client.post(
            "/v1/assistant/procurement-import/target/validate",
            headers=headers,
            json={
                "planId": plan["planId"],
                "spreadsheetUrl": "https://tenant.feishu.cn/sheets/SheetToken123",
                "sheetId": "sheetA",
            },
        )
        assert validated.status_code == 200, validated.text
        assert validated.json()["valid"] is True

        exported = client.get(
            "/v1/assistant/procurement-import/export",
            params={"planId": plan["planId"]},
        )
        assert exported.status_code == 200, exported.text
        workbook = load_workbook(BytesIO(exported.content), data_only=False)
        assert workbook.active["C2"].value == "GSH-CLOUD-TEST-001"
        assert workbook.active["D2"].value == "测试店铺-测试运营（二组）$"
        assert workbook.active["E2"].value == "测试运营"

        started = client.post(
            "/v1/assistant/procurement-import/sheet-sync",
            headers=headers,
            json={"planId": plan["planId"], "confirmWrite": True},
        )
        assert started.status_code == 202, started.text
        job_id = started.json()["jobId"]
        deadline = time.time() + 5
        status_payload = None
        while time.time() < deadline:
            status_response = client.get(
                "/v1/assistant/procurement-import/sheet-sync/status",
                params={"jobId": job_id},
            )
            assert status_response.status_code == 200, status_response.text
            status_payload = status_response.json()
            if status_payload["state"] in {"completed", "partial", "failed"}:
                break
            time.sleep(0.05)
        assert status_payload is not None
        assert status_payload["state"] == "completed", status_payload
        assert status_payload["rowsWritten"] == 1
        assert len(gateway.rows) == 1
        assert gateway.rows[0][1][gateway.headers.index("店铺")] == "测试店铺-测试运营（二组）$"
        operator_index = gateway.headers.index("导入操作人")
        assert gateway.rows[0][1][operator_index] == "合成测试用户"

        with database.session_factory() as session:
            job = session.scalar(select(ProcurementImportJob))
            assert job is not None
            assert job.state == "completed"
            assert job.progress["errors"] == []
            audit_payload = json.dumps(
                [
                    {
                        "action": event.action,
                        "details": event.details,
                        "changeSummary": event.change_summary,
                    }
                    for event in session.scalars(select(AuditEvent))
                ],
                ensure_ascii=False,
            )
            assert "GSH-CLOUD-TEST-001" not in audit_payload
            assert "Recipient Test" not in audit_payload


def test_cloud_import_endpoint_is_fail_closed_when_feature_is_disabled(tmp_path) -> None:
    app, _database, _oauth = build_test_app(tmp_path)
    with TestClient(app) as client:
        login(client)
        response = client.post(
            "/v1/assistant/procurement-import/parse",
            headers={"X-Xynigo-Web-CSRF": "same-origin"},
            json={"filename": "x.xlsx", "contentBase64": "eA=="},
        )
        assert response.status_code == 503
        assert response.json()["detail"]["code"] == "procurement_import_cloud_disabled"


def test_cloud_parser_copy_matches_the_canonical_local_source() -> None:
    root = Path(__file__).resolve().parents[3]
    source = (root / "src/purchase_tool/procurement_import.py").read_text(
        encoding="utf-8"
    )
    expected = source.replace(
        "from .xlsx_cell_images import embed_cell_images",
        "from .procurement_import_xlsx import embed_cell_images",
    ).replace(
        "from .lark_sheet_sync import LarkCliSheetsGateway, LarkSheetSyncError",
        "from .procurement_import_sheet import FeishuSheetsGateway as LarkCliSheetsGateway, LarkSheetSyncError",
    )
    generated = (
        root / "cloud/auth-service/src/xynigo_auth/procurement_import_core.py"
    ).read_text(encoding="utf-8")
    assert generated.endswith(expected)
    assert (
        root / "cloud/auth-service/src/xynigo_auth/procurement_import_xlsx.py"
    ).read_bytes() == (root / "src/purchase_tool/xlsx_cell_images.py").read_bytes()


@pytest.mark.parametrize('same_batch', [False, True])
def test_cloud_reimport_skips_purchaser_children_without_overwriting(tmp_path, same_batch):
    workbook = load_workbook(BytesIO(source_workbook()))
    sheet = workbook.active
    original = [cell.value for cell in sheet[2]]
    payload = json.loads(original[19].split('[XYP2]')[1].split('[/XYP2]')[0])
    second = list(payload['i'][0])
    second[0], second[1], second[2], second[4] = (
        'SOURCE-02', '422790138', 'SYNTHETIC-SKU-02', 'Negro')
    payload['i'].append(second)
    remark = '[XYP2]' + json.dumps(payload) + '[/XYP2]'
    sheet.delete_rows(2)
    for index, item in enumerate(payload['i']):
        row = list(original)
        row[14], row[15], row[16], row[19] = (
            'ERP-SKU-%02d' % index, item[0] + ':' + item[4] + '-M', 75, remark)
        sheet.append(row)
    stream = BytesIO()
    workbook.save(stream)
    content = base64.b64encode(stream.getvalue()).decode('ascii')
    gateway = FakeCloudSheetGateway()
    app, _database, _oauth = build_test_app(
        tmp_path, procurement_import_enabled=True, procurement_import_gateway=gateway)
    headers = {'X-Xynigo-Web-CSRF': 'same-origin'}

    def import_workbook(client, filename):
        response = client.post('/v1/assistant/procurement-import/parse', headers=headers,
                               json={'filename': filename, 'contentBase64': content})
        assert response.status_code == 201, response.text
        plan = response.json()
        assert plan['detailCount'] == 2
        response = client.post('/v1/assistant/procurement-import/target/validate',
                               headers=headers, json={
                                   'planId': plan['planId'],
                                   'spreadsheetUrl': 'https://tenant.feishu.cn/sheets/SheetToken123',
                                   'sheetId': 'sheetA'})
        assert response.status_code == 200, response.text
        response = client.post('/v1/assistant/procurement-import/sheet-sync',
                               headers=headers,
                               json={'planId': plan['planId'], 'confirmWrite': True})
        assert response.status_code == 202, response.text
        job_id = response.json()['jobId']
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            response = client.get('/v1/assistant/procurement-import/sheet-sync/status',
                                  params={'jobId': job_id})
            assert response.status_code == 200, response.text
            status = response.json()
            if status['state'] in {'completed', 'partial', 'failed'}:
                assert status['state'] == 'completed', status
                return plan, status
            time.sleep(0.05)
        raise AssertionError('synthetic cloud import did not complete')

    with TestClient(app) as client:
        login(client)
        original_plan, status = import_workbook(client, 'synthetic_original.xlsx')
        assert status['rowsWritten'] == 2
        with gateway.lock:
            edited = []
            for index, (number, raw) in enumerate(gateway.rows):
                values = dict(zip(gateway.headers, raw))
                values['销售订单号'] += '-%d' % (index + 1)
                values.update({'采购员': '合成采购员', '采购状态': '已下单',
                               '采购订单号': 'SYNTH-PURCHASE-%d' % index})
                edited.append((number, tuple(values[name] for name in gateway.headers)))
                gateway.links[number] = 'https://example.com/purchaser-link'
                gateway.backgrounds[number] = '#ABCDEF'
            gateway.rows = tuple(edited)
        before = (gateway.rows, dict(gateway.links), dict(gateway.backgrounds))
        filename = 'synthetic_original.xlsx' if same_batch else 'synthetic_reexport.xlsx'
        new_plan, status = import_workbook(client, filename)
        assert (new_plan['importBatch'] == original_plan['importBatch']) == same_batch
        assert status['rowsWritten'] == 0
        assert status['rowsExisting'] == 2
        assert status['rowsStyled'] == status['linksWritten'] == status['written'] == 0
        assert (gateway.rows, gateway.links, gateway.backgrounds) == before


def _invalid_remark_batch(*, all_invalid=False):
    original = load_workbook(BytesIO(source_workbook()))
    values = list(original.active.values)
    headers = list(values[0])
    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = 'order_'
    worksheet.append(headers)
    for index in range(121):
        row = list(values[1])
        row[headers.index('订单号')] = 'SYNTH-ERR-%03d' % index
        row[headers.index('包裹号')] = 'SYNTH-PKG-%03d' % index
        if index or all_invalid:
            row[headers.index('客服备注')] = '[XYP2]{broken}[/XYP2]'
        worksheet.append(row)
    stream = BytesIO()
    workbook.save(stream)
    return base64.b64encode(stream.getvalue()).decode('ascii')


def test_cloud_preserves_all_diagnostics_and_blocks_legacy_validated_error_plan(tmp_path):
    gateway = FakeCloudSheetGateway()
    app, database, _ = build_test_app(tmp_path, procurement_import_enabled=True,
                                      procurement_import_gateway=gateway)
    headers = {'X-Xynigo-Web-CSRF': 'same-origin'}
    with TestClient(app) as client:
        login(client)
        parsed = client.post('/v1/assistant/procurement-import/parse', headers=headers,
            json={'filename': 'synthetic.xlsx', 'contentBase64': _invalid_remark_batch()})
        assert parsed.status_code == 201, parsed.text
        result = parsed.json()
        assert result['canImport'] is False
        assert result['totalOrderCount'] == 121
        assert result['successOrderCount'] == 1
        assert result['failedOrderCount'] == 120
        assert len(result['issues']) == 120
        assert result['issues'][-1]['rowNumbers'] == [122]
        validated = client.post('/v1/assistant/procurement-import/target/validate', headers=headers,
            json={'planId': result['planId'], 'sheetId': 'sheetA',
                  'spreadsheetUrl': 'https://tenant.feishu.cn/sheets/SheetToken123'})
        assert validated.status_code == 422
        exported = client.get('/v1/assistant/procurement-import/export', params={'planId': result['planId']})
        assert exported.status_code == 422
        runtime = app.state.procurement_import_service
        with database.session_factory() as session:
            stored = session.scalar(select(ProcurementImportPlan))
            plan = runtime._load_plan(stored)
            assert len(plan.issues) == 120
            plan.target = CollaborationSheetTarget(
                'https://tenant.feishu.cn/sheets/SheetToken123', 'sheetA', '合成工作表', 1)
            runtime._save_plan(stored, plan, status='validated')
            session.commit()
        started = client.post('/v1/assistant/procurement-import/sheet-sync', headers=headers,
                              json={'planId': result['planId'], 'confirmWrite': True})
        assert started.status_code == 422, started.text
        assert started.json()['detail']['code'] == 'procurement_import_blocked'
        assert len(started.json()['detail']['diagnostics']['issues']) == 120
        with database.session_factory() as session:
            assert session.scalar(select(ProcurementImportJob)) is None
        assert gateway.rows == ()


def test_cloud_all_failed_returns_complete_located_diagnostics_without_plan(tmp_path):
    gateway = FakeCloudSheetGateway()
    app, database, _ = build_test_app(tmp_path, procurement_import_enabled=True,
                                      procurement_import_gateway=gateway)
    with TestClient(app) as client:
        login(client)
        response = client.post('/v1/assistant/procurement-import/parse',
            headers={'X-Xynigo-Web-CSRF': 'same-origin'},
            json={'filename': 'synthetic.xlsx',
                  'contentBase64': _invalid_remark_batch(all_invalid=True)})
        assert response.status_code == 422, response.text
        diagnostics = response.json()['detail']['diagnostics']
        assert diagnostics['failedOrderCount'] == diagnostics['totalOrderCount'] == 121
        assert diagnostics['successOrderCount'] == 0
        assert len(diagnostics['issues']) == 121
        assert diagnostics['issues'][-1]['packageNo'] == 'SYNTH-PKG-120'
        assert diagnostics['issues'][-1]['rowNumbers'] == [122]
        with database.session_factory() as session:
            assert session.scalar(select(ProcurementImportPlan)) is None
        assert gateway.rows == ()


def test_explicit_partial_plan_survives_storage_and_reimport_only_adds_new_orders(tmp_path):
    gateway = FakeCloudSheetGateway()
    app, database, _ = build_test_app(tmp_path, procurement_import_enabled=True,
                                      procurement_import_gateway=gateway)
    headers = {'X-Xynigo-Web-CSRF': 'same-origin'}
    with TestClient(app) as client:
        login(client)
        for valid_orders in (1, 2):
            workbook = load_workbook(BytesIO(base64.b64decode(_invalid_remark_batch())))
            sheet = workbook.active
            remark_col = [cell.value for cell in sheet[1]].index('客服备注') + 1
            if valid_orders == 2:
                sheet.cell(3, remark_col).value = sheet.cell(2, remark_col).value
            stream = BytesIO()
            workbook.save(stream)
            response = client.post('/v1/assistant/procurement-import/parse', headers=headers,
                json={'filename': 'synthetic.xlsx',
                      'contentBase64': base64.b64encode(stream.getvalue()).decode('ascii'),
                      'allowPartial': True})
            assert response.status_code == 201, response.text
            result = response.json()
            assert result['canImport'] is True
            assert result['partialImportSelected'] is True
            assert result['successOrderCount'] == valid_orders
            assert len(result['issues']) == 121 - valid_orders
            with database.session_factory() as session:
                import uuid
                record = session.get(ProcurementImportPlan, uuid.UUID(result['planId']))
                plan = app.state.procurement_import_service._load_plan(record)
                assert plan.allow_partial is True
                assert len(plan.rows) == valid_orders
                assert len(plan.issues) == 121 - valid_orders
            validated = client.post('/v1/assistant/procurement-import/target/validate', headers=headers,
                json={'planId': result['planId'], 'sheetId': 'sheetA',
                      'spreadsheetUrl': 'https://tenant.feishu.cn/sheets/SheetToken123'})
            assert validated.status_code == 200, validated.text
            cancelled = client.post('/v1/assistant/procurement-import/sheet-sync', headers=headers,
                json={'planId': result['planId'], 'confirmWrite': False})
            assert cancelled.status_code == 409
            assert len(gateway.rows) == valid_orders - 1
            started = client.post('/v1/assistant/procurement-import/sheet-sync', headers=headers,
                json={'planId': result['planId'], 'confirmWrite': True})
            assert started.status_code == 202, started.text
            deadline = time.time() + 5
            while time.time() < deadline:
                status = client.get('/v1/assistant/procurement-import/sheet-sync/status',
                    params={'jobId': started.json()['jobId']}).json()
                if status['state'] in {'completed', 'failed', 'partial'}:
                    break
                time.sleep(0.02)
            assert status['state'] == 'completed', status
            assert status['rowsWritten'] == 1
            assert len(gateway.rows) == valid_orders
            order_col = gateway.headers.index('销售订单号')
            assert {row[order_col] for _, row in gateway.rows} == {
                'SYNTH-ERR-%03d' % index for index in range(valid_orders)}


def test_cloud_purchase_remark_is_authoritative_when_sales_specs_do_not_match(tmp_path):
    class NoExistingImagesGateway(FakeCloudSheetGateway):
        def image_presence(self, url, sheet_id, row_numbers, column='L'):
            return {int(row): False for row in row_numbers}

    workbook = load_workbook(BytesIO(source_workbook()))
    sheet = workbook.active
    columns = [cell.value for cell in sheet[1]]
    sheet.cell(2, columns.index('SKU') + 1).value = 'UNRELATED-SALES-SKU'
    sheet.cell(2, columns.index('产品规格') + 1).value = 'UNRELATED:White-XS'
    stream = BytesIO()
    workbook.save(stream)
    app, _, _ = build_test_app(tmp_path, procurement_import_enabled=True,
                               procurement_import_gateway=NoExistingImagesGateway())
    with TestClient(app) as client:
        login(client)
        response = client.post('/v1/assistant/procurement-import/parse',
            headers={'X-Xynigo-Web-CSRF': 'same-origin'},
            json={'filename': 'synthetic.xlsx',
                  'contentBase64': base64.b64encode(stream.getvalue()).decode('ascii')})
        assert response.status_code == 201, response.text
        result = response.json()
        assert result['canImport'] is True
        assert result['errorCount'] == 0
        assert result['preview'][0]['itemSalesAmount'] is None
        assert result['preview'][0]['orderImageReady'] is False
        assert result['preview'][0]['sourceUnmatched'] is True
        assert result['preview'][0]['mainSpec'] != 'White'
        assert result['warningCount'] == 1
        plan_id = result['planId']
        validated = client.post('/v1/assistant/procurement-import/target/validate',
            headers={'X-Xynigo-Web-CSRF': 'same-origin'},
            json={'planId': plan_id, 'sheetId': 'sheetA',
                  'spreadsheetUrl': 'https://tenant.feishu.cn/sheets/SheetToken123'})
        assert validated.status_code == 200, validated.text
        started = client.post('/v1/assistant/procurement-import/sheet-sync',
            headers={'X-Xynigo-Web-CSRF': 'same-origin'},
            json={'planId': plan_id, 'confirmWrite': True})
        assert started.status_code == 202, started.text
        deadline = time.time() + 5
        while time.time() < deadline:
            status = client.get('/v1/assistant/procurement-import/sheet-sync/status',
                params={'jobId': started.json()['jobId']}).json()
            if status['state'] in {'completed', 'failed', 'partial'}:
                break
            time.sleep(0.02)
        assert status['state'] == 'completed', status
        assert status['skippedUnmatched'] == 1
        assert status['rowsWritten'] == 1
        assert status['failed'] == 0
