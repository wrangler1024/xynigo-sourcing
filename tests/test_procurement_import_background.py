"""Optional order colors must not disable links/images or repaint history."""

import base64

import pytest

from purchase_tool.procurement_import import ProcurementImportError, ProcurementImportService
from purchase_tool.lark_sheet_sync import LarkSheetSyncError
from test_procurement_import import FakeSheetGateway, source_workbook, wait_for_sync


def setup_import(gateway=None):
    gateway = gateway or FakeSheetGateway()
    service = ProcurementImportService(sheet_gateway=gateway, sleep_fn=lambda _: None)
    parsed = service.parse('synthetic.xlsx', base64.b64encode(source_workbook()).decode())
    service.validate_target(parsed['planId'], 'https://tenant.feishu.cn/sheets/SheetToken123', 'sheetA')
    return service, gateway, parsed['planId']


def run(service, plan_id, enabled):
    job = service.start_sheet_sync(plan_id, confirm_write=True, fill_order_background=enabled)
    return wait_for_sync(service, job['jobId'])


def test_disabled_color_still_writes_rows_links_images_and_height_without_reading_colors():
    class Gateway(FakeSheetGateway):
        def row_backgrounds(self, *args):
            raise AssertionError('disabled colors must not depend on color reads')
    service, gateway, plan_id = setup_import(Gateway())
    result = run(service, plan_id, False)
    assert result['state'] == 'completed', result
    assert result['rowsWritten'] == result['written'] == result['linksWritten'] == 2
    assert result['fillOrderBackground'] is False
    assert result['rowsStyled'] == 0
    assert gateway.presentation_calls[0]['bands'] == ()
    assert gateway.presentation_calls[0]['height'] == 52
    assert len(gateway.links) == len(gateway.images) == 2
    # A later completed-plan retry with the switch on cannot paint existing rows.
    result = run(service, plan_id, True)
    assert result['state'] == 'completed', result
    assert result['rowsWritten'] == result['rowsStyled'] == 0


@pytest.mark.parametrize('enabled', [False, True])
def test_failure_retry_keeps_original_choice_and_only_its_new_rows(enabled):
    class Gateway(FakeSheetGateway):
        fail_once = True
        def apply_row_presentation(self, *args, **kwargs):
            if self.fail_once:
                self.fail_once = False
                raise LarkSheetSyncError('synthetic format interruption')
            return super().apply_row_presentation(*args, **kwargs)
    service, gateway, plan_id = setup_import(Gateway())
    failed = run(service, plan_id, enabled)
    assert failed['state'] == 'failed'
    assert failed['rowsWritten'] == 2
    assert failed['backgroundPlanIndices'] == [0, 1]
    result = run(service, plan_id, not enabled)
    assert result['state'] == 'completed', result
    assert result['fillOrderBackground'] is enabled
    assert result['rowsWritten'] == 0
    assert result['written'] == 2
    assert result['rowsStyled'] == (2 if enabled else 0)
    assert len(gateway.append_calls) == 1


def test_non_boolean_color_setting_is_rejected():
    service, _gateway, plan_id = setup_import()
    with pytest.raises(ProcurementImportError, match='背景色'):
        run(service, plan_id, 'false')
