"""Synthetic regression cases for purchaser-edited child order numbers."""

import base64
from copy import deepcopy

import pytest

from purchase_tool.procurement_import import OUTPUT_HEADERS, ProcurementImportService
from purchase_tool.system_order_key import create_system_order_key, legacy_order_key
from test_procurement_import import FakeSheetGateway, source_workbook, wait_for_sync


def split_order_case(*, same_batch=False, key_format='ok1', split=True):
    gateway = FakeSheetGateway()
    service = ProcurementImportService(
        sheet_gateway=gateway, sleep_fn=lambda _seconds: None)
    parsed = service.parse(
        'synthetic_reimport.xlsx', base64.b64encode(source_workbook()).decode('ascii'))
    plan = service.pending[parsed['planId']]
    rows = []
    for index, row in enumerate(plan.rows):
        values = dict(row.values)
        if key_format == 'legacy':
            values['系统订单键'] = legacy_order_key(
                values['店铺'], values['销售订单号'], values['包裹号'])
        elif key_format == 'missing':
            values['系统订单键'] = ''
        if split:
            values['销售订单号'] += '-%d' % (index + 1)
        if not same_batch:
            values['导入批次'] = 'synthetic_previous_batch'
        values.update({'采购员': '合成采购员', '采购状态': '已下单',
                       '采购订单号': 'SYNTH-PURCHASE-%d' % index,
                       '实际付款': 35, '异常备注': '合成执行备注'})
        rows.append((10 + index, tuple(values[name] for name in OUTPUT_HEADERS)))
        # Re-import must not replace manually maintained links, images or colors.
        gateway.backgrounds[10 + index] = '#ABCDEF'
        gateway.links[10 + index] = 'https://example.com/purchaser-link'
    gateway.rows = tuple(rows)
    service.validate_target(
        parsed['planId'], 'https://tenant.feishu.cn/sheets/SheetToken123', 'sheetA')
    return service, gateway, plan


def edit_row(gateway, index, changes):
    rows = list(gateway.rows)
    number, raw = rows[index]
    values = dict(zip(gateway.headers, raw))
    values.update(changes)
    rows[index] = (number, tuple(values[name] for name in gateway.headers))
    gateway.rows = tuple(rows)


def sync(service, plan):
    started = service.start_sheet_sync(plan.plan_id, confirm_write=True)
    return wait_for_sync(service, started['jobId'])


def assert_no_order_writes(gateway, before):
    assert gateway.rows == before
    assert gateway.append_calls == []
    assert gateway.presentation_calls == []
    assert gateway.hyperlink_calls == []
    assert gateway.writes == []


@pytest.mark.parametrize('same_batch', [False, True])
@pytest.mark.parametrize('key_format', ['ok1', 'legacy'])
def test_reimport_skips_manual_children_and_preserves_procurement(same_batch, key_format):
    service, gateway, plan = split_order_case(
        same_batch=same_batch, key_format=key_format)
    before = gateway.rows
    completed = sync(service, plan)
    assert completed['state'] == 'completed', completed
    assert completed['rowsWritten'] == 0
    assert completed['rowsExisting'] == 2
    assert completed['rowsStyled'] == completed['linksWritten'] == completed['written'] == 0
    assert_no_order_writes(gateway, before)
    # Retrying this plan again must remain idempotent.
    assert sync(service, plan)['rowsWritten'] == 0
    assert_no_order_writes(gateway, before)


@pytest.mark.parametrize('same_batch', [False, True])
@pytest.mark.parametrize('changes', [
    {'需求数量': 99}, {'主规格': 'Different'}, {'次规格': 'XXL'},
    {'采购指导价': 999}, {'采购链接': 'https://www.shein.com.mx/x-p-9999999.html#sku=changed'},
    {'店铺': '另一测试店铺'}, {'包裹号': 'OTHER-SYNTH-PACKAGE'},
    {'销售订单号': 'UNRELATED-SYNTH-ORDER'},
])
def test_manual_children_with_real_conflicts_block_all_order_writes(same_batch, changes):
    service, gateway, plan = split_order_case(same_batch=same_batch)
    edit_row(gateway, 0, changes)
    before = gateway.rows
    failed = sync(service, plan)
    assert failed['state'] == 'failed', failed
    assert_no_order_writes(gateway, before)


@pytest.mark.parametrize('same_batch', [False, True])
def test_manual_child_notes_are_preserved_without_order_writes(same_batch):
    service, gateway, plan = split_order_case(same_batch=same_batch)
    edit_row(gateway, 0, {'采购备注': '没货，采购现场反馈'})
    before = gateway.rows
    completed = sync(service, plan)
    assert completed['state'] == 'completed'
    assert completed['rowsExisting'] == 2
    assert_no_order_writes(gateway, before)


@pytest.mark.parametrize('same_batch', [False, True])
def test_partial_manually_split_order_is_not_treated_as_a_resumable_upload(same_batch):
    service, gateway, plan = split_order_case(same_batch=same_batch)
    gateway.rows = gateway.rows[:1]
    before = gateway.rows
    failed = sync(service, plan)
    assert failed['state'] == 'failed', failed
    assert_no_order_writes(gateway, before)


@pytest.mark.parametrize('same_batch', [False, True])
def test_missing_parent_key_on_possible_children_blocks_instead_of_guessing(same_batch):
    service, gateway, plan = split_order_case(
        same_batch=same_batch, key_format='missing')
    before = gateway.rows
    failed = sync(service, plan)
    assert failed['state'] == 'failed', failed
    assert '系统订单键' in failed['error']
    assert '10' in failed['error']
    assert_no_order_writes(gateway, before)


def test_missing_key_on_unchanged_legacy_rows_still_deduplicates():
    service, gateway, plan = split_order_case(key_format='missing', split=False)
    before = gateway.rows
    completed = sync(service, plan)
    assert completed['state'] == 'completed', completed
    assert completed['rowsWritten'] == 0
    assert completed['rowsExisting'] == 2
    assert_no_order_writes(gateway, before)


@pytest.mark.parametrize('saved_key', ['damaged-key', create_system_order_key('x', 'y', 'z')])
def test_inconsistent_saved_identity_blocks_with_sheet_row(saved_key):
    service, gateway, plan = split_order_case()
    edit_row(gateway, 0, {'系统订单键': saved_key})
    before = gateway.rows
    failed = sync(service, plan)
    assert failed['state'] == 'failed', failed
    assert '系统订单键' in failed['error']
    assert '10' in failed['error']
    assert_no_order_writes(gateway, before)


def test_a_real_order_with_numeric_suffix_is_not_merged_with_its_prefix():
    service, gateway, plan = split_order_case()
    for index, (_number, raw) in enumerate(gateway.rows):
        values = dict(zip(gateway.headers, raw))
        # These are independently imported source identities, not manual children.
        edit_row(gateway, index, {'系统订单键': create_system_order_key(
            values['店铺'], values['销售订单号'], values['包裹号'])})
    before = gateway.rows
    completed = sync(service, plan)
    assert completed['state'] == 'completed', completed
    assert completed['rowsWritten'] == 2
    assert completed['rowsExisting'] == 0
    assert gateway.rows[:2] == before


def test_nested_child_suffix_is_accepted_only_when_original_key_proves_parent():
    service, gateway, plan = split_order_case()
    edit_row(gateway, 0, {'销售订单号': plan.rows[0].values['销售订单号'] + '-1-2'})
    before = gateway.rows
    completed = sync(service, plan)
    assert completed['state'] == 'completed', completed
    assert completed['rowsWritten'] == 0
    assert_no_order_writes(gateway, before)


@pytest.mark.parametrize('same_batch', [False, True])
def test_only_one_renamed_detail_still_preserves_the_whole_order(same_batch):
    service, gateway, plan = split_order_case(same_batch=same_batch)
    edit_row(gateway, 0, {'销售订单号': plan.rows[0].values['销售订单号']})
    before = gateway.rows
    completed = sync(service, plan)
    assert completed['state'] == 'completed', completed
    assert completed['rowsExisting'] == 2
    assert_no_order_writes(gateway, before)


@pytest.mark.parametrize('same_batch', [False, True])
@pytest.mark.parametrize('conflict', [False, True])
def test_mixed_new_and_split_orders_append_only_new_unless_any_conflict(same_batch, conflict):
    service, gateway, plan = split_order_case(same_batch=same_batch)
    new_row = deepcopy(plan.rows[0])
    new_row.values['销售订单号'] = 'SYNTH-NEW-ORDER'
    new_row.values['包裹号'] = 'SYNTH-NEW-PACKAGE'
    new_row.values['系统订单键'] = create_system_order_key(
        new_row.values['店铺'], new_row.values['销售订单号'], new_row.values['包裹号'])
    plan.rows.insert(0, new_row)
    plan.order_count += 1
    if conflict:
        edit_row(gateway, 0, {'需求数量': 99})
    before = gateway.rows
    completed = sync(service, plan)
    if conflict:
        assert completed['state'] == 'failed', completed
        assert_no_order_writes(gateway, before)
    else:
        assert completed['state'] == 'completed', completed
        assert completed['rowsWritten'] == 1
        assert completed['rowsExisting'] == 2
        assert gateway.rows[:2] == before
        assert len(gateway.rows) == 3
        assert [item[1] for item in gateway.writes] == [12]
        assert gateway.links[10] == gateway.links[11] == 'https://example.com/purchaser-link'
        assert gateway.backgrounds[10] == gateway.backgrounds[11] == '#ABCDEF'


@pytest.mark.parametrize('same_batch', [False, True])
def test_duplicated_child_detail_blocks_import(same_batch):
    service, gateway, plan = split_order_case(same_batch=same_batch)
    gateway.rows += ((12, gateway.rows[0][1]),)
    before = gateway.rows
    failed = sync(service, plan)
    assert failed['state'] == 'failed', failed
    assert_no_order_writes(gateway, before)
