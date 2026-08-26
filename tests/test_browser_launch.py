# -*- coding: utf-8 -*-
import unittest

from purchase_tool.cloud_auth import DEFAULT_AUTH_BASE_URL
from purchase_tool.main import browser_launch_url


class _CloudClient(object):
    def __init__(self, base_url):
        self.base_url = base_url


class _AuthService(object):
    def __init__(self, base_url):
        self.client = _CloudClient(base_url)


class BrowserLaunchTests(unittest.TestCase):
    def test_desktop_default_opens_configured_cloud_workspace(self):
        self.assertEqual(
            browser_launch_url(
                'http://127.0.0.1:8765',
                [],
                _AuthService('https://workspace.example.test/'),
            ),
            'https://workspace.example.test',
        )

    def test_local_ui_flag_keeps_local_compatibility_entry(self):
        self.assertEqual(
            browser_launch_url(
                'http://127.0.0.1:8767',
                ['--local-ui'],
                _AuthService('https://workspace.example.test'),
            ),
            'http://127.0.0.1:8767',
        )

    def test_missing_auth_client_uses_safe_default_cloud_origin(self):
        self.assertEqual(
            browser_launch_url('http://127.0.0.1:8765', []),
            DEFAULT_AUTH_BASE_URL,
        )


if __name__ == '__main__':
    unittest.main()
