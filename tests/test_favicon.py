# -*- coding: utf-8 -*-
from pathlib import Path
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer

from purchase_tool.main import Handler, X_ICON_ICO


class FaviconRouteTests(unittest.TestCase):
    def setUp(self):
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(
            target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def test_root_favicon_returns_final_x_icon(self):
        url = 'http://127.0.0.1:%d/favicon.ico?v=5' % (
            self.server.server_address[1])
        with urllib.request.urlopen(url, timeout=3) as response:
            body = response.read()
            content_type = response.headers.get_content_type()
        self.assertEqual(content_type, 'image/x-icon')
        self.assertEqual(body, Path(X_ICON_ICO).read_bytes())
        self.assertEqual(int.from_bytes(body[4:6], 'little'), 7)

    def test_static_frontend_is_never_served_from_browser_cache(self):
        url = 'http://127.0.0.1:%d/?ui=organization-access' % (
            self.server.server_address[1])
        with urllib.request.urlopen(url, timeout=3) as response:
            body = response.read().decode('utf-8')
            cache_control = response.headers.get('Cache-Control')
        self.assertEqual(cache_control, 'no-store')
        self.assertIn('data-module="organizationaccess"', body)


if __name__ == '__main__':
    unittest.main()
