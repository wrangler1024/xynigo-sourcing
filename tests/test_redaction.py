# -*- coding: utf-8 -*-
import unittest

from purchase_tool.main import public_error
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

    def test_scrubs_feishu_deployment_and_record_identifiers(self):
        identifiers = (
            'recPublicExampleOnly', 'bascnPublicExample123',
            'tblPublicExample123', 'cli_public_example_123')
        safe = scrub_text(' '.join(identifiers))
        for value in identifiers:
            self.assertNotIn(value, safe)

    def test_public_api_error_uses_last_resort_scrubbing(self):
        safe = public_error(RuntimeError(
            'password=hunter2 buyer@example.test '
            'tblPublicExample123 cli_public_example_123'))
        for value in ('hunter2', 'buyer@example.test',
                      'tblPublicExample123', 'cli_public_example_123'):
            self.assertNotIn(value, safe)


if __name__ == '__main__':
    unittest.main()
