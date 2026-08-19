# -*- coding: utf-8 -*-
import base64
import unittest

from purchase_tool.cdp import PageTarget


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
