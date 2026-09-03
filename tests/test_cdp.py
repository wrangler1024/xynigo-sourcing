# -*- coding: utf-8 -*-
import base64
import unittest

from purchase_tool.cdp import CdpClient, PageTarget


class CdpVersionTests(unittest.TestCase):
    def test_version_info_returns_only_browser_and_protocol(self):
        client = CdpClient.__new__(CdpClient)
        client._http = lambda path: {
            'Browser': 'HeadlessChrome/148.0.0.0',
            'Protocol-Version': '1.3',
            'User-Agent': 'sensitive-full-fingerprint',
            'webSocketDebuggerUrl': 'ws://127.0.0.1/devtools/browser/private',
        } if path == '/json/version' else {}

        self.assertEqual(client.version_info(), {
            'browser': 'HeadlessChrome/148.0.0.0',
            'protocolVersion': '1.3',
        })


class CdpScreenshotTests(unittest.TestCase):
    def test_capture_element_union_uses_clipped_compressed_jpeg(self):
        page = PageTarget.__new__(PageTarget)
        page._evaluate = lambda _expr: {
            'x': 10.0, 'y': 20.0, 'width': 1008.0, 'height': 535.0}
        calls = []

        def send(method, params=None):
            calls.append((method, params))
            return {'data': base64.b64encode(b'jpeg-bytes').decode('ascii')}

        page._send = send
        data, width, height = page.capture_element_union(
            ['.carrier', '.timeline'], quality=75,
            hide_selectors=['.address'])
        self.assertEqual(data, b'jpeg-bytes')
        self.assertEqual((width, height), (1008, 535))
        method, params = calls[-1]
        self.assertEqual(method, 'Page.captureScreenshot')
        self.assertEqual(params['format'], 'jpeg')
        self.assertEqual(params['quality'], 75)
        self.assertTrue(params['captureBeyondViewport'])
        self.assertEqual(params['clip']['scale'], 1)


if __name__ == '__main__':
    unittest.main()
