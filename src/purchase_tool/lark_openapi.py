# -*- coding: utf-8 -*-
"""Minimal Feishu OpenAPI client for the Xynigo buyer ledger.

The client uses an enterprise custom app (tenant_access_token), keeps the
access token in memory only, and is intentionally transport-injectable so all
write-path tests can run against FakeLark without real Feishu access.
"""
from dataclasses import dataclass
import json
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
import urllib.request

from .redaction import scrub_text


DEFAULT_ORIGIN = 'https://open.feishu.cn'
TOKEN_PATH = '/open-apis/auth/v3/tenant_access_token/internal'
RETRYABLE_CODES = {1254291}
TOKEN_INVALID_CODES = {99991663, 99991671}
RETRYABLE_HTTP_STATUS = {429, 500, 502, 503, 504}


class LarkApiError(Exception):
    def __init__(self, message, code=None, retryable=False):
        super().__init__(message)
        self.code = code
        self.retryable = bool(retryable)


@dataclass
class LarkHttpResponse:
    status: int
    body: bytes


class UrllibLarkTransport(object):
    def __init__(self, opener=None):
        self.opener = opener or urllib.request.build_opener()

    def request(self, method, url, headers, body, timeout):
        request = urllib.request.Request(
            url, data=body, headers=headers, method=method)
        try:
            with self.opener.open(request, timeout=timeout) as response:
                return LarkHttpResponse(
                    int(response.status), response.read(2 * 1024 * 1024))
        except HTTPError as exc:
            return LarkHttpResponse(
                int(exc.code), exc.read(2 * 1024 * 1024))
        except (URLError, TimeoutError, OSError) as exc:
            raise LarkApiError('飞书 OpenAPI 网络连接失败', retryable=True) from exc


def _safe_message(value, secrets=()):
    text = str(value or '')
    for secret in secrets:
        if secret:
            text = text.replace(str(secret), '<redacted>')
    return scrub_text(text)[:200]


class LarkOpenApiClient(object):
    """Direct OpenAPI client scoped to one configured Base table."""

    def __init__(self, credential_provider, base_token, table_id,
                 transport=None, origin=DEFAULT_ORIGIN, timeout=20,
                 sleep_fn=time.sleep, clock=time.time, batch_size=200):
        self.credential_provider = credential_provider
        self.base_token = str(base_token or '').strip()
        self.table_id = str(table_id or '').strip()
        self.transport = transport or UrllibLarkTransport()
        self.origin = str(origin or DEFAULT_ORIGIN).rstrip('/')
        self.timeout = float(timeout)
        self.sleep = sleep_fn
        self.clock = clock
        self.batch_size = max(1, min(200, int(batch_size)))
        self._token = ''
        self._token_expires_at = 0.0
        self._token_lock = threading.Lock()

    def _credentials(self):
        credentials = self.credential_provider()
        if credentials is None:
            raise LarkApiError('尚未配置小犀代采飞书应用凭证')
        app_id = str(getattr(credentials, 'app_id', '') or '').strip()
        app_secret = str(getattr(credentials, 'app_secret', '') or '').strip()
        if not app_id or not app_secret:
            raise LarkApiError('小犀代采飞书应用凭证不完整')
        return app_id, app_secret

    def _decode(self, response, secrets=()):
        try:
            payload = json.loads((response.body or b'{}').decode('utf-8'))
        except Exception as exc:
            raise LarkApiError(
                '飞书 OpenAPI 返回了非 JSON 结果',
                retryable=response.status in RETRYABLE_HTTP_STATUS) from exc
        if not isinstance(payload, dict):
            raise LarkApiError('飞书 OpenAPI 返回结构无效')
        code = payload.get('code', 0 if 200 <= response.status < 300 else response.status)
        try:
            numeric_code = int(code)
        except (TypeError, ValueError):
            numeric_code = code
        if 200 <= response.status < 300 and numeric_code == 0:
            return payload
        message = _safe_message(
            payload.get('msg') or payload.get('message') or '请求失败',
            secrets)
        retryable = (response.status in RETRYABLE_HTTP_STATUS
                     or numeric_code in RETRYABLE_CODES)
        raise LarkApiError(
            '飞书 OpenAPI 请求失败（错误码 %s）：%s' %
            (numeric_code, message or '未知错误'),
            code=numeric_code, retryable=retryable)

    def _fetch_token_locked(self):
        if getattr(self.transport, 'cloud_managed', False):
            self._token = 'cloud-managed'
            self._token_expires_at = self.clock() + 3600
            return self._token
        app_id, app_secret = self._credentials()
        body = json.dumps({
            'app_id': app_id, 'app_secret': app_secret,
        }, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
        response = self.transport.request(
            'POST', self.origin + TOKEN_PATH,
            {'Content-Type': 'application/json; charset=utf-8'},
            body, self.timeout)
        payload = self._decode(response, (app_id, app_secret))
        token = str(payload.get('tenant_access_token') or '')
        if not token:
            raise LarkApiError('飞书令牌响应缺少 tenant_access_token')
        try:
            expires_in = max(60, int(payload.get('expire') or 7200))
        except (TypeError, ValueError):
            expires_in = 7200
        self._token = token
        self._token_expires_at = self.clock() + max(30, expires_in - 120)
        return token

    def tenant_access_token(self):
        with self._token_lock:
            if self._token and self.clock() < self._token_expires_at:
                return self._token
            return self._fetch_token_locked()

    def _clear_token(self):
        with self._token_lock:
            self._token = ''
            self._token_expires_at = 0.0

    def _url(self, path, query=None, require_target=True):
        if require_target and (not self.base_token or not self.table_id):
            raise LarkApiError('尚未配置飞书买家号台账目标')
        suffix = ('?' + urlencode(query, doseq=True)) if query else ''
        return self.origin + path + suffix

    def _request(self, method, path, payload=None, query=None,
                 authenticated=True, require_target=True):
        body = (json.dumps(payload, ensure_ascii=False,
                           separators=(',', ':')).encode('utf-8')
                if payload is not None else None)
        token_refreshed = False
        last_error = None
        for attempt in range(5):
            headers = {'Content-Type': 'application/json; charset=utf-8'}
            if authenticated:
                headers['Authorization'] = 'Bearer ' + self.tenant_access_token()
            try:
                response = self.transport.request(
                    method, self._url(path, query, require_target),
                    headers, body, self.timeout)
                result = self._decode(
                    response, (self.base_token, self.table_id, self._token))
                return result
            except LarkApiError as exc:
                last_error = exc
                if (authenticated and exc.code in TOKEN_INVALID_CODES
                        and not token_refreshed):
                    token_refreshed = True
                    self._clear_token()
                    continue
                if not exc.retryable or attempt >= 4:
                    raise
                self.sleep(min(8, 2 ** attempt))
        raise last_error or LarkApiError('飞书 OpenAPI 请求失败')

    @property
    def _table_path(self):
        return '/open-apis/bitable/v1/apps/%s/tables/%s' % (
            quote(self.base_token, safe=''), quote(self.table_id, safe=''))

    def get_wiki_node(self, node_token):
        result = self._request(
            'GET', '/open-apis/wiki/v2/spaces/get_node',
            query={'token': str(node_token or '').strip()},
            require_target=False)
        node = (result.get('data') or {}).get('node')
        if not isinstance(node, dict):
            raise LarkApiError('飞书 Wiki 节点返回结构无效')
        return node

    def get_target_metadata(self):
        """Return non-sensitive display names for the configured Base/table."""
        base_path = '/open-apis/bitable/v1/apps/%s' % quote(
            self.base_token, safe='')
        base_result = self._request('GET', base_path)
        base = (base_result.get('data') or {}).get('app')
        if not isinstance(base, dict) or not str(base.get('name') or '').strip():
            raise LarkApiError('飞书多维表格元数据返回结构无效')

        tables_path = base_path + '/tables'
        tables, page_token = [], ''
        while True:
            query = {'page_size': 100}
            if page_token:
                query['page_token'] = page_token
            table_result = self._request(
                'GET', tables_path, query=query)
            data = table_result.get('data') or {}
            page = data.get('items') or []
            if not isinstance(page, list):
                raise LarkApiError('飞书数据表列表返回结构无效')
            tables.extend(page)
            if not data.get('has_more'):
                break
            next_token = str(data.get('page_token') or '')
            if not next_token or next_token == page_token:
                raise LarkApiError('飞书数据表列表分页停滞')
            page_token = next_token
        table = next((item for item in tables
                      if isinstance(item, dict)
                      and str(item.get('table_id') or '') == self.table_id), None)
        if not table:
            raise LarkApiError(
                '小犀代采应用无法访问目标数据表；请在多维表格高级权限中为应用开放该表')
        if not str(table.get('name') or '').strip():
            raise LarkApiError('飞书目标数据表名称为空')
        return {
            'base_name': str(base['name']).strip(),
            'table_name': str(table['name']).strip(),
        }

    def list_fields(self):
        items, page_token = [], ''
        while True:
            query = {'page_size': 100}
            if page_token:
                query['page_token'] = page_token
            result = self._request(
                'GET', self._table_path + '/fields', query=query)
            data = result.get('data') or {}
            page = data.get('items') or []
            if not isinstance(page, list):
                raise LarkApiError('飞书字段列表返回结构无效')
            items.extend(page)
            if not data.get('has_more'):
                return items
            next_token = str(data.get('page_token') or '')
            if not next_token or next_token == page_token:
                raise LarkApiError('飞书字段列表分页停滞')
            page_token = next_token

    def list_records(self, field_names=None):
        items, page_token = [], ''
        while True:
            query = {'page_size': 500}
            if field_names:
                query['field_names'] = json.dumps(
                    list(field_names), ensure_ascii=False,
                    separators=(',', ':'))
            if page_token:
                query['page_token'] = page_token
            result = self._request(
                'GET', self._table_path + '/records', query=query)
            data = result.get('data') or {}
            page = data.get('items') or []
            if not isinstance(page, list):
                raise LarkApiError('飞书记录列表返回结构无效')
            items.extend(page)
            if not data.get('has_more'):
                return items
            next_token = str(data.get('page_token') or '')
            if not next_token or next_token == page_token:
                raise LarkApiError('飞书记录列表分页停滞')
            page_token = next_token

    def get_record(self, record_id):
        result = self._request(
            'GET', self._table_path + '/records/%s' %
            quote(str(record_id or ''), safe=''))
        record = (result.get('data') or {}).get('record')
        if not isinstance(record, dict):
            raise LarkApiError('飞书记录回读结构无效')
        return record

    def get_spreadsheet_values(self, spreadsheet_token, range_a1):
        """Read one Feishu spreadsheet range without binding this client to it.

        Resource Center uses a normal spreadsheet for the proxy ledger while
        the original buyer ledger uses Base.  Keeping this read method on the
        same credential-aware client avoids a second token cache and, more
        importantly, lets callers request non-contiguous safe ranges so proxy
        usernames/passwords are not loaded for ordinary list pages.
        """
        spreadsheet_token = str(spreadsheet_token or '').strip()
        range_a1 = str(range_a1 or '').strip()
        if not spreadsheet_token or not range_a1:
            raise LarkApiError('飞书电子表格读取目标不完整')
        path = '/open-apis/sheets/v2/spreadsheets/%s/values/%s' % (
            quote(spreadsheet_token, safe=''), quote(range_a1, safe=''))
        result = self._request('GET', path, require_target=False)
        value_range = (result.get('data') or {}).get('valueRange')
        if not isinstance(value_range, dict):
            raise LarkApiError('飞书电子表格返回结构无效')
        values = value_range.get('values') or []
        if not isinstance(values, list):
            raise LarkApiError('飞书电子表格单元格结果无效')
        return values

    def batch_create(self, field_maps):
        created = []
        for offset in range(0, len(field_maps), self.batch_size):
            batch = field_maps[offset:offset + self.batch_size]
            result = self._request(
                'POST', self._table_path + '/records/batch_create',
                {'records': [{'fields': dict(fields)} for fields in batch]})
            records = (result.get('data') or {}).get('records') or []
            if len(records) != len(batch):
                raise LarkApiError('飞书批量新增返回记录数量不一致')
            created.extend(records)
        return created

    def batch_update(self, updates):
        records = [
            {'record_id': record_id, 'fields': dict(fields)}
            for record_id, fields in updates
        ]
        updated = []
        for offset in range(0, len(records), self.batch_size):
            batch = records[offset:offset + self.batch_size]
            result = self._request(
                'POST', self._table_path + '/records/batch_update',
                {'records': batch})
            response_records = (result.get('data') or {}).get('records') or []
            if response_records and len(response_records) != len(batch):
                raise LarkApiError('飞书批量更新返回记录数量不一致')
            updated.extend(response_records or batch)
        return updated
