# -*- coding: utf-8 -*-
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INDEX_HTML = ROOT / 'src' / 'purchase_tool' / 'web' / 'index.html'


class BusinessLogsWebContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = INDEX_HTML.read_text(encoding='utf-8')

    def test_business_log_module_is_visible_as_a_real_system_feature(self):
        self.assertIn(
            'data-parent="system" data-module="businesslogs"', self.html)
        self.assertIn('id="businessLogsPanel"', self.html)
        self.assertIn("label: '业务操作日志', primary: 'system'", self.html)
        self.assertIn(
            "$('businessLogsPanel').classList.toggle('hidden', module !== 'businesslogs')",
            self.html,
        )
        self.assertNotIn(
            'data-parent="system" data-planned="业务操作日志"', self.html)

    def test_all_p0_filters_list_detail_and_pagination_are_static_contracts(self):
        for element_id in (
                'businessLogStartTime', 'businessLogEndTime',
                'businessLogModule', 'businessLogOperator',
                'businessLogResult', 'businessLogBusinessNo',
                'businessLogOperationType', 'businessLogRequestId',
                'businessLogTbody', 'businessLogDetailPanel',
                'btnBusinessLogPrev', 'btnBusinessLogNext',
                'businessLogPageSize'):
            with self.subTest(element_id=element_id):
                self.assertIn('id="%s"' % element_id, self.html)
        self.assertIn("return '/api/business-logs?' + params.toString()", self.html)
        self.assertIn(
            "api('/api/business-logs/' + encodeURIComponent(", self.html)
        self.assertIn('function renderBusinessLogRows(data)', self.html)
        self.assertIn('function renderBusinessLogDetail(data)', self.html)

    def test_frontend_states_scope_and_sensitive_log_prohibitions(self):
        self.assertIn('当前范围：仅本人业务日志', self.html)
        self.assertIn('本租户业务日志（主管 / 管理员审计范围）', self.html)
        for outcome in (
                'success', 'validation_failed', 'permission_denied',
                'business_conflict', 'not_found',
                'external_service_failed', 'failure'):
            self.assertIn('<option value="%s">' % outcome, self.html)
        self.assertIn(
            '日志不会保存密码、Cookie、Token、API Key、完整电话或详细地址',
            self.html,
        )
        self.assertNotIn("api('/api/business-logs', { method: 'POST'", self.html)


if __name__ == '__main__':
    unittest.main()
