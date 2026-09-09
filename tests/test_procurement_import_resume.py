"""Synthetic reproduction of the 260-row append/read-back/continue sequence."""
import base64
from copy import deepcopy
import pytest
from purchase_tool.procurement_import import ProcurementImportService, OUTPUT_HEADERS
from purchase_tool.procurement_image_fetch import ProcurementImageError
from purchase_tool.purchase_link_cell import purchase_url_cell_matches
from purchase_tool.system_order_key import create_system_order_key
from test_procurement_import import FakeSheetGateway, source_workbook, wait_for_sync, JPEG


class AutoLinkGateway(FakeSheetGateway):
    reject_auto_links = False

    def hyperlink_presence(self, url, sheet_id, expected_links, column='M'):
        return {number: (not self.reject_auto_links and purchase_url_cell_matches(
            [{'cellPosition': None, 'type': 'url', 'text': self.links.get(number),
              'link': self.links.get(number)}], link))
            for number, link in expected_links.items()}


def make_service(gateway, fetcher=None):
    service = ProcurementImportService(sheet_gateway=gateway,
        sleep_fn=lambda _: None, image_fetcher=fetcher)
    result = service.parse('synthetic.xlsx', base64.b64encode(source_workbook()).decode())
    return service, service.pending[result['planId']]


def start(service, plan):
    if plan.target is None:
        service.validate_target(plan.plan_id, 'https://tenant.feishu.cn/sheets/SheetToken123', 'sheetA')
    job = service.start_sheet_sync(plan.plan_id, confirm_write=True, fill_order_background=False)
    return wait_for_sync(service, job['jobId'])


def test_260_rows_resume_without_duplicates_preserving_notes_and_skipping_unmatched():
    gateway = AutoLinkGateway()
    downloads = []

    def fetch(url):
        downloads.append(url)
        return JPEG

    service, plan = make_service(gateway, fetch)
    template = plan.rows[0]
    plan.rows = []
    plan.import_batch = 'synthetic-260'
    for index in range(260):
        row = deepcopy(template)
        group = index // 2 if index < 20 else index - 10
        order, package = 'SYNTH-ORDER-%03d' % group, 'SYNTH-PACK-%03d' % group
        row.values.update({'销售订单号': order, '包裹号': package,
            '系统订单键': create_system_order_key(row.values['店铺'], order, package),
            '导入批次': plan.import_batch})
        row.order_group_index = group
        row.order_image = JPEG if index < 254 else b''
        row.source_unmatched = 254 <= index < 259
        row.order_image_url = 'https://img.ltwebstatic.com/test/%d.png' % index
        plan.rows.append(row)
    plan.order_count, plan.source_rows = 250, 260
    gateway.reject_auto_links = True
    first = start(service, plan)
    assert first['state'] == 'failed' and first['rowsWritten'] == 260
    assert first['total'] == 0 and len(gateway.rows) == 260
    assert not gateway.writes and not downloads
    first_append_count = len(gateway.append_calls)
    assert sum(len(call['rows']) for call in gateway.append_calls) == 260

    number, raw = gateway.rows[0]
    changed = list(raw)
    changed[OUTPUT_HEADERS.index('采购备注')] = '没货'
    gateway.rows = ((number, tuple(changed)),) + gateway.rows[1:]
    expected_rows = deepcopy(gateway.rows)
    gateway.reject_auto_links = False
    resumed = start(service, plan)
    assert resumed['state'] == 'completed', resumed
    assert resumed['rowsWritten'] == 0 and resumed['rowsExisting'] == 260
    assert resumed['written'] == 255 and resumed['skippedUnmatched'] == 5
    assert resumed['failed'] == 0 and resumed['processed'] == 260
    assert downloads == ['https://img.ltwebstatic.com/test/259.png']
    assert gateway.rows == expected_rows
    assert len(gateway.append_calls) == first_append_count

    again = start(service, plan)
    assert again['state'] == 'completed' and again['rowsWritten'] == again['written'] == 0
    assert again['skippedExisting'] == 255 and len(downloads) == 1
    assert gateway.rows == expected_rows
    assert len(gateway.append_calls) == first_append_count


def test_image_download_failure_is_per_row_and_other_images_continue():
    def unavailable(url):
        raise ProcurementImageError('读取订单图片网址失败，请稍后续传')

    gateway = AutoLinkGateway()
    service, plan = make_service(gateway, unavailable)
    plan.rows[0].order_image = b''
    result = start(service, plan)
    assert result['state'] == 'partial'
    assert result['rowsWritten'] == 2 and result['failed'] == result['written'] == 1
    assert result['missingSource'] == 1
    assert '读取订单图片网址失败' in result['errors'][0]['message']


def test_historical_manual_note_is_preserved_while_new_order_is_imported():
    gateway = AutoLinkGateway()
    service, plan = make_service(gateway)
    previous = []
    for index, row in enumerate(plan.rows):
        values = {**row.values, '导入批次': 'older-batch', '采购备注': '没货'}
        previous.append((8 + index, tuple(values[name] for name in OUTPUT_HEADERS)))
    gateway.rows = tuple(previous)
    new_row = deepcopy(plan.rows[0])
    new_row.values.update({'销售订单号': 'SYNTH-NEW', '包裹号': 'SYNTH-PACK-NEW',
        '系统订单键': create_system_order_key(new_row.values['店铺'], 'SYNTH-NEW', 'SYNTH-PACK-NEW')})
    plan.rows.append(new_row)
    plan.order_count = 2
    result = start(service, plan)
    assert result['state'] == 'completed' and result['rowsWritten'] == 1
    assert result['rowsExisting'] == 2 and result['written'] == 1
    assert gateway.rows[:2] == tuple(previous)


@pytest.mark.parametrize('field,new_value', [
    ('采购链接', 'https://www.shein.com.mx/x-p-9999999.html#sku=changed'),
    ('需求数量', 3), ('主规格', 'Different color'), ('次规格', 'Different size'),
    ('采购指导价', 0.01),
])
@pytest.mark.parametrize('same_batch', [True, False])
def test_changed_purchase_facts_still_block_before_any_append(field, new_value, same_batch):
    gateway = AutoLinkGateway()
    service, plan = make_service(gateway)
    previous = []
    for index, row in enumerate(plan.rows):
        values = {**row.values, '采购备注': '人工采购反馈'}
        if not same_batch:
            values['导入批次'] = 'older-batch'
        if index == 0:
            values[field] = new_value
        previous.append((8 + index, tuple(values[name] for name in OUTPUT_HEADERS)))
    gateway.rows = tuple(previous)
    result = start(service, plan)
    assert result['state'] == 'failed'
    assert not gateway.append_calls and not gateway.writes
