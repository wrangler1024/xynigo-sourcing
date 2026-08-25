# -*- coding: utf-8 -*-
"""Public release audit rules for runtime artifacts and test addresses."""

from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))

from audit_public_release import SKIP_DIRS, is_allowed_ip  # noqa: E402


class PublicReleaseAuditTests(unittest.TestCase):
    def test_allows_loopback_and_rfc_documentation_addresses(self):
        self.assertTrue(is_allowed_ip('127.0.0.1'))
        self.assertTrue(is_allowed_ip('203.0.113.9'))
        self.assertTrue(is_allowed_ip('198.51.100.20'))

    def test_rejects_real_public_and_private_infrastructure_addresses(self):
        self.assertFalse(is_allowed_ip('8.8.' + '8.8'))
        self.assertFalse(is_allowed_ip('10.1.' + '0.7'))

    def test_skips_local_runtime_output_directories(self):
        self.assertIn('查询日志', SKIP_DIRS)
        self.assertIn('运行数据', SKIP_DIRS)


if __name__ == '__main__':
    unittest.main()
