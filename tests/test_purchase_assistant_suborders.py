"""Synthetic child-order grouping, lookup and recipient isolation."""

from copy import deepcopy

import pytest

from purchase_tool.purchase_assistant import (
    PurchaseAssistantError, find_recipient, rows_to_tasks, search_tasks,
)
from purchase_tool.system_order_key import create_system_order_key
from test_purchase_assistant import sample_row


def child_rows():
    parent = create_system_order_key('SYNTH-STORE', 'SYNTH-ORDER', 'SYNTH-PACKAGE')
    return [sample_row(**{
        '__row_number': str(20 + index), '系统订单键': parent,
        '店铺': 'SYNTH-STORE', '销售订单号': 'SYNTH-ORDER-%d' % index,
        '包裹号': 'SYNTH-PACKAGE', '主规格': spec, '需求数量': str(index),
        '地址1': 'Synthetic address %d' % index, 'CURP': 'SYNTHETIC-CURP-%d' % index,
    }) for index, spec in [(1, 'Black'), (2, 'White'), (3, 'Blue')]]


def test_each_child_is_searchable_and_reads_only_its_recipient():
    rows = child_rows()
    before = deepcopy(rows)
    tasks = rows_to_tasks(rows)
    assert len(tasks) == 3
    assert len({task['taskKey'] for task in tasks}) == 3
    for row in rows:
        matches, count = search_tasks(tasks, row['销售订单号'])
        assert count == 1
        task = matches[0]
        assert task['salesOrderNo'] == row['销售订单号']
        assert task['quantity'] == row['需求数量']
        assert task['specSummary'] == row['主规格'] + ' / ' + row['次规格']
        assert task['sourceOrderKey'] == row['系统订单键']
        assert task['taskKey'].isascii() and len(task['taskKey']) <= 300
        assert not any(field in task for field in ('地址1', 'recipientName', 'CURP', 'curp'))
        recipient = find_recipient(iter(rows), task['taskKey'])
        assert recipient['addressLine1'] == row['地址1']
        assert recipient['curp'] == row['CURP']
    assert rows == before, 'lookup must not change source keys or business rows'
    assert search_tasks(tasks, rows[0]['系统订单键'])[1] == 3
    assert search_tasks(tasks, 'SYNTH-PACKAGE')[1] == 3


def test_child_keys_survive_row_movement_and_removal_of_a_sibling():
    rows = child_rows()
    original = {task['salesOrderNo']: task['taskKey'] for task in rows_to_tasks(rows)}
    moved = [dict(row, __row_number=str(index + 2)) for index, row in enumerate(reversed(rows))]
    assert {task['salesOrderNo']: task['taskKey'] for task in rows_to_tasks(moved)} == original
    remaining = [rows[1]]
    assert rows_to_tasks(remaining)[0]['taskKey'] == original[rows[1]['销售订单号']]
    assert find_recipient(remaining, original[rows[1]['销售订单号']])['addressLine1'] == rows[1]['地址1']


def test_details_of_same_child_aggregate_without_including_siblings():
    rows = child_rows()
    rows.append(dict(rows[1], __row_number='30', 主规格='Second detail'))
    tasks = rows_to_tasks(rows)
    assert len(tasks) == 3
    match = search_tasks(tasks, 'SYNTH-ORDER-2')[0][0]
    assert 'Second detail' in match['specSummary']
    assert 'Black' not in match['specSummary'] and 'Blue' not in match['specSummary']
    assert find_recipient(rows, match['taskKey'])['addressLine1'] == rows[1]['地址1']


def test_ambiguous_old_parent_key_requires_reselection_even_with_same_address():
    rows = child_rows()
    for row in rows:
        row['地址1'] = 'One shared address'
        row['CURP'] = ''
    with pytest.raises(PurchaseAssistantError, match='重新搜索'):
        find_recipient(rows, rows[0]['系统订单键'])


def test_unambiguous_old_task_keys_remain_compatible():
    row = sample_row()
    assert find_recipient(iter([row]), row['系统订单键'])['addressLine1'] == row['地址1']
    row['系统订单键'] = ''
    old_key = row['销售订单号'] + '|' + row['包裹号']
    assert find_recipient(iter([row]), old_key)['addressLine1'] == row['地址1']


@pytest.mark.parametrize('field', ['店铺', '包裹号'])
@pytest.mark.parametrize('has_system_key', [False, True])
def test_same_sales_number_in_different_source_identity_stays_separate(field, has_system_key):
    first = sample_row()
    if not has_system_key:
        first['系统订单键'] = ''
    second = dict(first, __row_number='3')
    second[field] = 'SYNTH-OTHER'
    second['地址1'] = 'Other synthetic address'
    tasks = rows_to_tasks([first, second])
    assert len(tasks) == 2
    assert find_recipient([first, second], tasks[1]['taskKey'])['addressLine1'] == second['地址1']


def test_child_address_conflicts_still_block_and_empty_child_cannot_borrow_address():
    rows = child_rows()
    task = search_tasks(rows_to_tasks(rows), 'SYNTH-ORDER-2')[0][0]
    conflict = dict(rows[1], __row_number='30', 地址1='Different complete address')
    with pytest.raises(PurchaseAssistantError, match='多组不同'):
        find_recipient(rows + [conflict], task['taskKey'])
    rows[1]['地址1'] = ''
    with pytest.raises(PurchaseAssistantError, match='缺少完整'):
        find_recipient(rows, task['taskKey'])


def test_stale_child_key_after_renaming_never_falls_back_to_parent():
    rows = child_rows()
    old_key = search_tasks(rows_to_tasks(rows), 'SYNTH-ORDER-2')[0][0]['taskKey']
    rows[1]['销售订单号'] = 'SYNTH-ORDER-2-1'
    with pytest.raises(PurchaseAssistantError, match='未找到'):
        find_recipient(rows, old_key)


def test_assistant_child_tasks_and_import_parent_deduplication_coexist():
    import base64
    from purchase_tool.procurement_import import OUTPUT_HEADERS, ProcurementImportService
    from test_procurement_import import FakeSheetGateway, source_workbook

    gateway = FakeSheetGateway()
    importer = ProcurementImportService(sheet_gateway=gateway)
    parsed = importer.parse('synthetic-reimport.xlsx', base64.b64encode(source_workbook()).decode())
    plan = importer.pending[parsed['planId']]
    rows = []
    for index, detail in enumerate(plan.rows):
        values = dict(detail.values)
        values['销售订单号'] += '-%d' % (index + 1)
        values['导入批次'] = 'synthetic-previous-batch'
        values['__row_number'] = str(index + 20)
        rows.append(values)
    gateway.rows = tuple((int(row['__row_number']), tuple(row[name] for name in OUTPUT_HEADERS)) for row in rows)
    before = deepcopy(gateway.rows)
    importer.validate_target(parsed['planId'], 'https://tenant.feishu.cn/sheets/SheetToken123', 'sheetA')
    tasks = rows_to_tasks(rows)
    assert len(tasks) == 2
    assert tasks[0]['taskKey'] != tasks[1]['taskKey']
    assert tasks[0]['sourceOrderKey'] == tasks[1]['sourceOrderKey']
    for row in rows:
        matched, count = search_tasks(tasks, row['销售订单号'])
        assert count == 1
        assert find_recipient(rows, matched[0]['taskKey'])['addressLine1'] == row['地址1']
    state = importer._target_batch_state(plan, plan.target)
    assert state[5] == [], 'reimport must append no duplicate parent rows'
    assert sum(state[3].values()) == 2
    assert gateway.rows == before
