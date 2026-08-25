import re
import unittest
from pathlib import Path


INDEX_HTML = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "purchase_tool"
    / "web"
    / "index.html"
)


class SystemLogsWebContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = INDEX_HTML.read_text(encoding="utf-8")

    def test_system_management_has_permission_gated_runtime_log_entry(self):
        self.assertIn(
            'data-parent="system" data-module="systemlogs"', self.html
        )
        self.assertRegex(
            self.html,
            r"systemlogs:\s*\{[\s\S]*?requiredPermission:\s*"
            r"'system\.runtime_log\.read'",
        )
        self.assertIn("系统运行日志查看", self.html)
        self.assertIn("'system.runtime_log.read'", self.html)

    def test_runtime_log_panel_has_all_p1_filters_list_detail_and_pagination(self):
        for element_id in (
            "systemLogsPanel",
            "systemLogStartTime",
            "systemLogEndTime",
            "systemLogCategory",
            "systemLogLevel",
            "systemLogService",
            "systemLogComponent",
            "systemLogEventType",
            "systemLogStatusCode",
            "systemLogRequestId",
            "systemLogKeyword",
            "systemLogTbody",
            "systemLogDetailPanel",
            "btnSystemLogPrev",
            "btnSystemLogNext",
            "systemLogPageSize",
        ):
            self.assertIn(f'id="{element_id}"', self.html)

    def test_runtime_log_query_and_detail_use_read_only_same_origin_routes(self):
        self.assertIn("return '/api/system-logs?' + params.toString()", self.html)
        self.assertIn(
            "api('/api/system-logs/' + encodeURIComponent(", self.html
        )
        self.assertNotRegex(
            self.html,
            r"api\('/api/system-logs[^']*',\s*\{\s*method:\s*"
            r"'(?:POST|PUT|PATCH|DELETE)'",
        )
        for parameter in ("startTime", "endTime", "page", "pageSize"):
            self.assertRegex(self.html, rf"params\.set\('{re.escape(parameter)}'")
        for parameter, element_id in (
            ("category", "systemLogCategory"),
            ("level", "systemLogLevel"),
            ("service", "systemLogService"),
            ("component", "systemLogComponent"),
            ("eventType", "systemLogEventType"),
            ("statusCode", "systemLogStatusCode"),
            ("requestId", "systemLogRequestId"),
            ("keyword", "systemLogKeyword"),
        ):
            self.assertIn(f"['{parameter}', '{element_id}']", self.html)

    def test_runtime_log_ui_states_privacy_and_retention_boundary(self):
        for text in (
            "不保存查询串、请求/响应正文、原始堆栈",
            "当前范围：本租户系统日志",
            "retentionDays",
            "maxRowsPerTenant",
            "错误指纹",
            "requestId",
            "traceId",
        ):
            self.assertIn(text, self.html)
        self.assertIn(
            "$('systemLogsPanel').classList.toggle('hidden', module !== 'systemlogs')",
            self.html,
        )
        self.assertIn(
            "module === 'systemlogs' && !systemLogsLoaded", self.html
        )


if __name__ == "__main__":
    unittest.main()
