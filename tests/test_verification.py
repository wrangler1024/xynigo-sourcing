# -*- coding: utf-8 -*-
import unittest

from purchase_tool.verification import (
    extract_outlook_security_code, extract_shein_code)


class VerificationParsingTests(unittest.TestCase):
    def test_outlook_code_anchors_body_not_headers(self):
        raw = (
            'Received: from host20260819.example\nDate: 19 Aug 2026\n\n'
            '你的一次性代码为: 481726\n')
        self.assertEqual(extract_outlook_security_code(raw), '481726')

    def test_outlook_code_missing_anchor_returns_none(self):
        self.assertIsNone(extract_outlook_security_code(
            'Received: 123456\nDate: 20260819'))

    def test_shein_code_parses_spanish_chinese_and_english(self):
        cases = [
            ('Tu código de verificación es 731904', '731904'),
            ('您的验证码为：2392，有效10分钟', '2392'),
            ('Your verification code is 882145', '882145'),
        ]
        for text, expected in cases:
            self.assertEqual(extract_shein_code(text), expected)

    def test_shein_code_prefers_reading_pane_after_old_preview(self):
        text = (
            '旧邮件预览 验证码为：2392\n'
            '阅读窗 Tu código de verificación es 731904')
        self.assertEqual(extract_shein_code(text), '731904')


if __name__ == '__main__':
    unittest.main()
