# -*- coding: utf-8 -*-
"""验证码提供者：号商 API + 半人工兜底。"""
import re
import time
import urllib.request


class VerificationError(Exception):
    pass


# 邮件验证接口可能返回原始邮件源码；只锚定正文，不扫全文数字。
RE_MS_CODE = re.compile(
    r'你的一次性代码为\s*[:：]\s*(\d{4,8})', re.I)

RE_SHEIN_CODE_PATTERNS = [
    re.compile(r'验证码(?:为|是)?\s*[:：]?\s*(\d{4,8})', re.I),
    re.compile(
        r'c[oó]digo(?:\s+de\s+verificaci[oó]n)?'
        r'(?:\s+es|\s*[:：])?[^0-9]{0,80}(\d{4,8})', re.I),
    re.compile(
        r'verification\s+code(?:\s+is|\s*[:：])?'
        r'[^0-9]{0,80}(\d{4,8})', re.I),
]


def extract_outlook_security_code(raw_mail):
    matches = RE_MS_CODE.findall(raw_mail or '')
    return matches[-1] if matches else None


def extract_shein_code(mail_text):
    candidates = []
    for pattern in RE_SHEIN_CODE_PATTERNS:
        candidates.extend((match.start(), match.group(1))
                          for match in pattern.finditer(mail_text or ''))
    # Outlook 全页文本会同时包含邮件列表预览与右侧阅读窗；
    # 阅读窗在 DOM 后部，取最后一个锚定命中可避免抓到旧预览码。
    return max(candidates, default=(None, None), key=lambda item: item[0])[1]


class VerificationProvider(object):
    def peek_outlook_security_code(self):
        return None

    def get_outlook_security_code(self, previous_code=None, timeout=120):
        raise NotImplementedError


class HttpCodeProvider(VerificationProvider):
    """Generic HTTP verification-code endpoint; the URL is never logged."""

    def __init__(self, code_api_url, interval=10, opener=None):
        if not str(code_api_url or '').startswith(('http://', 'https://')):
            raise ValueError('接码 API 必须是 HTTP(S) URL')
        self._url = code_api_url
        self.interval = max(2, int(interval))
        self._opener = opener or urllib.request.build_opener()

    def __repr__(self):
        return '<HttpCodeProvider url=<redacted>>'

    def _fetch(self):
        req = urllib.request.Request(
            self._url, headers={'User-Agent': 'purchase-tool/0.3'})
        with self._opener.open(req, timeout=20) as resp:
            return resp.read().decode('utf-8', 'replace')

    def peek_outlook_security_code(self):
        try:
            return extract_outlook_security_code(self._fetch())
        except Exception:
            return None

    def get_outlook_security_code(self, previous_code=None, timeout=120):
        deadline = time.time() + max(1, int(timeout))
        last_error = None
        while time.time() < deadline:
            try:
                code = extract_outlook_security_code(self._fetch())
                if code and code != previous_code:
                    return code
            except Exception as exc:
                last_error = exc
            time.sleep(self.interval)
        suffix = '' if last_error is None else '（最后一次请求失败）'
        raise VerificationError('等待微软安全码超时%s' % suffix)


class ConsoleProvider(VerificationProvider):
    """半自动兜底：仅本地控制台读入，不回显其他凭证。"""

    def get_outlook_security_code(self, previous_code=None, timeout=120):
        code = input('请输入微软 4-8 位安全码: ').strip()
        if not re.fullmatch(r'\d{4,8}', code):
            raise VerificationError('微软安全码格式不正确')
        return code
