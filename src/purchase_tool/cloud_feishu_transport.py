# -*- coding: utf-8 -*-
"""Route executor Feishu reads through the tenant-owned cloud credential."""

from __future__ import annotations

import json
from urllib.parse import parse_qsl, urlparse

from .lark_openapi import LarkApiError, LarkHttpResponse
from .purchase_assistant import PurchaseAssistantError


class CloudFeishuTransport(object):
    cloud_managed = True

    def __init__(self, cloud_request, permission, legacy_clearer=None):
        if not callable(cloud_request):
            raise ValueError('云端飞书代理尚未就绪')
        self.cloud_request = cloud_request
        self.permission = str(permission or '')
        self.legacy_clearer = legacy_clearer

    @staticmethod
    def _target(url):
        parsed = urlparse(str(url or ''))
        if (parsed.scheme != 'https'
                or parsed.hostname not in ('open.feishu.cn', 'open.larksuite.com')
                or parsed.fragment):
            raise ValueError('飞书代理只允许访问官方 OpenAPI')
        return parsed.path, dict(parse_qsl(parsed.query, keep_blank_values=True))

    def _read(self, method, url):
        if str(method or '').upper() != 'GET':
            raise ValueError('云端飞书代理仅支持只读请求')
        path, query = self._target(url)
        result = self.cloud_request(path, query, self.permission)
        if callable(self.legacy_clearer):
            try:
                self.legacy_clearer()
            except Exception:
                pass
        return result

    def request_json(self, method, url, headers=None, payload=None, timeout=15.0):
        del headers, timeout
        if payload is not None:
            raise PurchaseAssistantError('云端飞书只读请求不能携带正文')
        try:
            return self._read(method, url)
        except PurchaseAssistantError:
            raise
        except Exception as exc:
            raise PurchaseAssistantError(str(exc) or '云端飞书代理请求失败') from exc

    def request(self, method, url, headers, body, timeout):
        del headers, timeout
        if body not in (None, b''):
            raise LarkApiError('云端飞书只读请求不能携带正文')
        try:
            payload = self._read(method, url)
            return LarkHttpResponse(
                200,
                json.dumps(payload, ensure_ascii=False, separators=(',', ':')).encode('utf-8'),
            )
        except LarkApiError:
            raise
        except Exception as exc:
            raise LarkApiError(
                str(exc) or '云端飞书代理请求失败', retryable=True
            ) from exc
