import json
import unittest
from purchase_tool.purchase_assistant import find_recipient, rows_from_values, rows_to_tasks, PurchaseAssistantError

SAMPLE_CURP = 'TESTCURP0000000001'  # Synthetic test marker, never sent to a live form.
def row(**extra):
    return {'销售订单号':'TEST-CURP-001','收货人姓名':'Lucia Prueba','收货人电话':'+52 477 000 0001','地址1':'Calle Prueba 100','地址2':'','收货人城市':'Guanajuato','收货人州/省':'Guanajuato','邮编':'36000',**extra}
class CurpRecipientContractTests(unittest.TestCase):
    def test_missing_and_empty_columns_are_distinct(self):
        self.assertEqual(find_recipient([row()], 'TEST-CURP-001|')['curpStatus'],'missing_column')
        self.assertEqual(find_recipient([row(CURP='')], 'TEST-CURP-001|')['curpStatus'],'empty')
    def test_case_and_empty_duplicate_rows_do_not_make_false_conflicts(self):
        result=find_recipient([row(CURP=''),row(curp='  '+SAMPLE_CURP.lower()+'  ')], 'TEST-CURP-001|')
        self.assertEqual(result['curp'],SAMPLE_CURP)
        self.assertEqual(result['curpStatus'],'provided')
    def test_conflict_does_not_return_an_identifier_or_block_the_address(self):
        result=find_recipient([row(CURP=SAMPLE_CURP),row(CURP='TESTCURP0000000002')], 'TEST-CURP-001|')
        self.assertEqual(result['curpStatus'],'conflict')
        self.assertEqual(result['curp'],'')
        self.assertEqual(result['recipientName'],'Lucia Prueba')
        self.assertNotIn(SAMPLE_CURP,json.dumps(result))
    def test_task_summaries_never_include_curp(self):
        text=json.dumps(rows_to_tasks([row(CURP=SAMPLE_CURP)]))
        self.assertNotIn(SAMPLE_CURP,text)
        self.assertTrue(all('curp' not in key.lower() for task in rows_to_tasks([row(CURP=SAMPLE_CURP)]) for key in task))
    def test_header_case_is_canonical_and_duplicate_columns_fail(self):
        self.assertEqual(rows_from_values([['销售订单号','curp'],['TEST',SAMPLE_CURP]])[0]['CURP'],SAMPLE_CURP)
        with self.assertRaises(PurchaseAssistantError):rows_from_values([['销售订单号','curp','CURP']])
