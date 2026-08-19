# -*- coding: utf-8 -*-
import unittest

from purchase_tool.redaction import mask_email, mask_url, scrub_text


class RedactionTests(unittest.TestCase):
    def test_masks_credentials(self):
        self.assertEqual(mask_email('buyer123@outlook.com'),
                         'bu***@outlook.com')
        self.assertEqual(mask_url(
            'https://example.test/get?mail=a%40b.com&key=secret'),
            'https://example.test/get')
        safe = scrub_text(
            'email=buyer123@outlook.com password=hunter2 '
            'url=https://example.test/get?key=secret')
        self.assertNotIn('buyer123', safe)
        self.assertNotIn('hunter2', safe)
        self.assertNotIn('key=secret', safe)


if __name__ == '__main__':
    unittest.main()
