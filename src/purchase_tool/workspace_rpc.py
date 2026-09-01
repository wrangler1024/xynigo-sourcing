# -*- coding: utf-8 -*-
"""Internal adapter from the outbound device channel to local capabilities."""

from __future__ import annotations

import base64
import json
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlparse
from urllib.request import ProxyHandler, Request, build_opener


MAX_RPC_RESPONSE_BYTES = 28 * 1024 * 1024
SLOW_MUTATION_PATHS = frozenset({
    '/api/query',
    '/api/envbatch/parse',
    '/api/envbatch/cloud-plan',
    '/api/envbatch/start',
    '/api/envbatch/backup/start',
    '/api/buyer-library/import/parse',
    '/api/buyer-library/import/commit',
})


class WorkspaceRpcClient(object):
    def __init__(self, base_url, rpc_token, opener=None, timeout=30.0):
        parsed = urlparse(str(base_url or ''))
        if (parsed.scheme != 'http'
                or parsed.hostname not in ('127.0.0.1', 'localhost')
                or not parsed.port
                or parsed.path not in ('', '/')
                or parsed.query or parsed.fragment):
            raise ValueError('执行器内部接口地址无效')
        self.base_url = str(base_url).rstrip('/')
        self.rpc_token = str(rpc_token or '')
        if len(self.rpc_token) < 32:
            raise ValueError('执行器内部接口凭证无效')
        # Windows Internet Settings may expose a system proxy to urllib.
        # Executor-internal loopback traffic must never leave this computer.
        self.opener = opener or build_opener(ProxyHandler({})).open
        self.timeout = float(timeout)

    def execute(self, payload):
        if not isinstance(payload, dict):
            raise ValueError('云端工作台任务无效')
        method = str(payload.get('method') or '').upper()
        target = str(payload.get('path') or '')
        parsed = urlparse(target)
        if (method not in ('GET', 'POST') or parsed.scheme or parsed.netloc
                or not parsed.path.startswith('/api/')
                or parsed.path.startswith('//') or parsed.fragment):
            raise ValueError('云端工作台任务地址无效')
        body = payload.get('body')
        if method == 'GET' and body not in (None, {}):
            raise ValueError('云端工作台 GET 任务不能携带正文')
        data = None
        headers = {
            'Accept': 'application/json, application/octet-stream;q=0.9',
            'X-Xynigo-Executor-RPC': self.rpc_token,
            'X-Xynigo-Source': 'executor_workspace_rpc',
        }
        if method == 'POST':
            data = json.dumps(
                body if isinstance(body, dict) else {},
                ensure_ascii=False,
                separators=(',', ':'),
            ).encode('utf-8')
            headers['Content-Type'] = 'application/json'
        request = Request(
            urljoin(self.base_url + '/', target.lstrip('/')),
            data=data,
            headers=headers,
            method=method,
        )
        try:
            response = self.opener(
                request, timeout=self._request_timeout(method, parsed.path))
            return self._read_response(response)
        except HTTPError as exc:
            return self._read_response(exc)
        except (URLError, TimeoutError, OSError) as exc:
            raise RuntimeError('执行器内部业务接口不可用') from exc

    def _request_timeout(self, method, path):
        if method == 'POST' and path in SLOW_MUTATION_PATHS:
            return max(self.timeout, 120.0)
        return self.timeout

    @staticmethod
    def _read_response(response):
        with response:
            raw = response.read(MAX_RPC_RESPONSE_BYTES + 1)
            if len(raw) > MAX_RPC_RESPONSE_BYTES:
                raise RuntimeError('执行器内部业务响应过大')
            status = int(getattr(response, 'status', None)
                         or getattr(response, 'code', 0) or 0)
            content_type = str(response.headers.get('Content-Type') or '')
            disposition = str(
                response.headers.get('Content-Disposition') or '')
        summary = {
            'httpStatus': status,
            'contentType': content_type[:255],
        }
        if disposition:
            summary['contentDisposition'] = disposition[:1024]
        if 'application/json' in content_type:
            try:
                parsed = json.loads(raw.decode('utf-8')) if raw else {}
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise RuntimeError('执行器内部 JSON 响应无效') from exc
            summary['responseType'] = 'json'
            summary['body'] = parsed
        else:
            summary['responseType'] = 'base64'
            summary['bodyBase64'] = base64.b64encode(raw).decode('ascii')
        return summary
