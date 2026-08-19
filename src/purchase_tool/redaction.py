# -*- coding: utf-8 -*-
"""注册模块脱敏工具。

原始邮箱、密码、Cookie 和带查询参数的接码 URL 不进入进度日志。
"""
import re
from urllib.parse import urlsplit, urlunsplit


RE_EMAIL = re.compile(r'([A-Za-z0-9._%+\-]{1,64})@([A-Za-z0-9.\-]+)')


def mask_email(value):
    value = str(value or '')
    match = RE_EMAIL.fullmatch(value)
    if not match:
        return '***'
    local, domain = match.groups()
    shown = local[:2] if len(local) > 2 else local[:1]
    return '%s***@%s' % (shown, domain)


def mask_url(value):
    try:
        parts = urlsplit(str(value or ''))
        return urlunsplit((parts.scheme, parts.netloc, parts.path, '', ''))
    except Exception:
        return '***'


def scrub_text(value):
    """用于异常摘要：脱敏邮箱、URL query 和常见凭证字段。"""
    text = str(value or '')
    text = RE_EMAIL.sub(lambda m: mask_email(m.group(0)), text)
    text = re.sub(r'(https?://[^\s?]+)\?[^\s]+', r'\1?<redacted>', text)
    text = re.sub(
        r'(?i)(password|passwd|pwd|secret|token|cookie|api[_-]?key)'
        r'\s*[:=]\s*[^\s,;]+', r'\1=<redacted>', text)
    return text
