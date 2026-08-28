# -*- coding: utf-8 -*-
"""Read-only SHEIN purchase-assistant service hosted by Xynigo executor."""
from __future__ import annotations

import json
import re
import secrets
import socket
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen


RECIPIENT_FIELDS = (
    '收货人姓名', '收货人电话', '地址1', '地址2',
    '收货人城市', '收货人州/省', '邮编',
)
REQUIRED_RECIPIENT_INDEXES = (0, 1, 2, 4, 5, 6)
CELL_RANGE_PATTERN = re.compile(
    r'^[A-Z]{1,3}[1-9]\d*:[A-Z]{1,3}(?:[1-9]\d*)?$')
ALLOWED_API_HOSTS = {'open.feishu.cn', 'open.larksuite.com'}


class PurchaseAssistantError(RuntimeError):
    """Safe user-facing error which never contains credentials or rows."""


class TokenRejectedError(PurchaseAssistantError):
    pass


def normalize(value: Any) -> str:
    if value is None:
        return ''
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return re.sub(r'\s+', ' ', str(value)).strip()


def rows_from_values(values: Any) -> list[dict[str, str]]:
    if not isinstance(values, list) or not values:
        return []
    if not isinstance(values[0], list):
        raise PurchaseAssistantError('飞书表格返回的表头格式异常')
    headers = [normalize(value) for value in values[0]]
    if not headers or any(not header for header in headers):
        raise PurchaseAssistantError('协作表表头为空或不完整')
    if len(set(headers)) != len(headers):
        raise PurchaseAssistantError('协作表存在重复表头')
    rows = []
    for offset, raw_values in enumerate(values[1:], start=2):
        if not isinstance(raw_values, list):
            raise PurchaseAssistantError('飞书表格返回的数据行格式异常')
        padded = raw_values[:len(headers)] + [''] * max(
            0, len(headers) - len(raw_values))
        row = {
            header: normalize(padded[index])
            for index, header in enumerate(headers)
        }
        row['__row_number'] = str(offset)
        if any(value for key, value in row.items()
               if key != '__row_number'):
            rows.append(row)
    return rows


def task_key(row: dict[str, str]) -> str:
    explicit = normalize(row.get('系统订单键'))
    if explicit:
        return explicit
    sales_order = normalize(row.get('销售订单号'))
    package = normalize(row.get('包裹号'))
    return sales_order + '|' + package if sales_order else ''


def site_code(country: str) -> str:
    normalized = normalize(country).lower()
    if 'mex' in normalized or '墨西哥' in normalized:
        return 'MX'
    if ('united states' in normalized or '美国' in normalized
            or normalized == 'us'):
        return 'US'
    return normalize(country)


def rows_to_tasks(rows: Iterable[dict[str, str]]) -> list[dict[str, Any]]:
    tasks = {}
    for row in rows:
        key = task_key(row)
        if not key:
            continue
        specs = [normalize(row.get('主规格')), normalize(row.get('次规格'))]
        task = {
            'taskKey': key,
            'salesOrderNo': normalize(row.get('销售订单号')),
            'packageNo': normalize(row.get('包裹号')),
            'store': normalize(row.get('店铺')),
            'status': normalize(row.get('采购状态')),
            'site': site_code(row.get('收货人国家', '')),
            'specSummary': ' / '.join(value for value in specs if value),
            'quantity': normalize(row.get('需求数量')),
            'guidePrice': normalize(row.get('采购指导价')),
            'rowNumber': int(row.get('__row_number') or 0),
        }
        previous = tasks.get(key)
        if previous:
            previous['quantity'] = previous['quantity'] or task['quantity']
            if (task['specSummary']
                    and task['specSummary'] not in previous['specSummary']):
                previous['specSummary'] = '；'.join(
                    value for value in (
                        previous['specSummary'], task['specSummary']) if value)
            continue
        tasks[key] = task
    return sorted(tasks.values(), key=lambda item: item['rowNumber'])


def search_tasks(tasks: Iterable[dict[str, Any]], query: str,
                 limit: int = 20) -> tuple[list[dict[str, Any]], int]:
    wanted = normalize(query).casefold()
    if not wanted:
        return [], 0
    maximum = max(1, min(50, int(limit)))
    ranked = []
    for task in tasks:
        values = [
            normalize(task.get('salesOrderNo')),
            normalize(task.get('packageNo')),
            normalize(task.get('store')),
            normalize(task.get('taskKey')),
        ]
        folded = [value.casefold() for value in values if value]
        if not any(wanted in value for value in folded):
            continue
        rank = 0 if any(value == wanted for value in folded) else (
            1 if any(value.startswith(wanted) for value in folded) else 2)
        ranked.append((rank, int(task.get('rowNumber') or 0), task))
    ranked.sort(key=lambda item: (item[0], item[1]))
    return [item[2] for item in ranked[:maximum]], len(ranked)


def find_recipient(rows: Iterable[dict[str, str]],
                   requested_key: str) -> dict[str, str]:
    wanted = normalize(requested_key)
    matched = [row for row in rows if task_key(row) == wanted]
    if not matched:
        raise PurchaseAssistantError('未找到对应的采购任务')
    signatures = {
        tuple(normalize(row.get(field)) for field in RECIPIENT_FIELDS)
        for row in matched
    }
    complete = {
        signature for signature in signatures
        if all(signature[index] for index in REQUIRED_RECIPIENT_INDEXES)
    }
    if not complete:
        raise PurchaseAssistantError('当前任务缺少完整收件信息')
    if len(complete) != 1:
        raise PurchaseAssistantError(
            '同一任务存在多组不同的完整收件信息，已停止自动填写')
    row = dict(zip(RECIPIENT_FIELDS, next(iter(complete))))
    return {
        'recipientName': normalize(row.get('收货人姓名')),
        'recipientPhone': normalize(row.get('收货人电话')),
        'addressLine1': normalize(row.get('地址1')),
        'addressLine2': normalize(row.get('地址2')),
        'city': normalize(row.get('收货人城市')),
        'stateProvince': normalize(row.get('收货人州/省')),
        'postalCode': normalize(row.get('邮编')),
    }


@dataclass(frozen=True)
class PurchaseAssistantConfig:
    spreadsheet_token: str
    sheet_id: str
    cell_range: str = 'A1:AQ'
    api_base: str = 'https://open.feishu.cn/open-apis'
    cache_ttl_seconds: float = 8.0

    @classmethod
    def from_runtime_config(cls, mapping: dict[str, Any]):
        token = normalize(mapping.get('purchaseAssistantSpreadsheetToken'))
        sheet_id = normalize(mapping.get('purchaseAssistantSheetId'))
        cell_range = normalize(
            mapping.get('purchaseAssistantCellRange') or 'A1:AQ').upper()
        api_base = normalize(
            mapping.get('purchaseAssistantApiBase')
            or 'https://open.feishu.cn/open-apis').rstrip('/')
        ttl = float(mapping.get('purchaseAssistantCacheTtlSeconds') or 8)
        if not token or not sheet_id:
            raise PurchaseAssistantError(
                '未配置采购执行协作表 token 和 sheet_id')
        if not CELL_RANGE_PATTERN.fullmatch(cell_range):
            raise PurchaseAssistantError('采购助手表格范围格式无效')
        parsed = urlparse(api_base)
        if (parsed.scheme != 'https' or parsed.hostname not in ALLOWED_API_HOSTS
                or parsed.path != '/open-apis'):
            raise PurchaseAssistantError('采购助手只允许访问飞书官方 OpenAPI')
        if ttl < 0 or ttl > 60:
            raise PurchaseAssistantError('采购助手缓存时间必须在 0 到 60 秒')
        return cls(token, sheet_id, cell_range, api_base, ttl)


class FeishuTransport(object):
    def request_json(self, method: str, url: str,
                     headers: Optional[dict[str, str]] = None,
                     payload: Optional[dict[str, Any]] = None,
                     timeout: float = 15.0) -> dict[str, Any]:
        body = (json.dumps(payload, ensure_ascii=False).encode('utf-8')
                if payload is not None else None)
        request_headers = {'Accept': 'application/json', **(headers or {})}
        if body is not None:
            request_headers['Content-Type'] = 'application/json; charset=utf-8'
        request = Request(
            url, data=body, headers=request_headers, method=method)
        try:
            with urlopen(request, timeout=timeout) as response:
                raw = response.read()
        except HTTPError as exc:
            try:
                error_payload = json.loads(exc.read().decode('utf-8'))
            except (UnicodeError, json.JSONDecodeError, OSError):
                error_payload = None
            if isinstance(error_payload, dict) and 'code' in error_payload:
                return error_payload
            if exc.code in {401, 403}:
                raise TokenRejectedError(
                    '飞书 API 身份校验或表格权限不通过') from exc
            raise PurchaseAssistantError(
                '飞书 API 请求失败（HTTP %s）' % exc.code) from exc
        except (URLError, socket.timeout, TimeoutError, OSError) as exc:
            raise PurchaseAssistantError('飞书 API 网络请求失败') from exc
        try:
            decoded = json.loads(raw.decode('utf-8'))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise PurchaseAssistantError('飞书 API 返回非法 JSON') from exc
        if not isinstance(decoded, dict):
            raise PurchaseAssistantError('飞书 API 返回格式异常')
        return decoded


class PurchaseAssistantSheetProvider(object):
    def __init__(self, config: PurchaseAssistantConfig,
                 credential_getter: Callable[[], Any],
                 transport: Optional[FeishuTransport] = None):
        self.config = config
        self.credential_getter = credential_getter
        self.transport = transport or FeishuTransport()
        self._token = ''
        self._token_expires_at = 0.0
        self._cached_rows = None
        self._cached_at = 0.0
        self._lock = threading.RLock()

    def _invalidate_token(self):
        self._token = ''
        self._token_expires_at = 0.0

    def _get_token(self):
        now = time.monotonic()
        if self._token and now < self._token_expires_at:
            return self._token
        credentials = self.credential_getter()
        app_id = normalize(getattr(credentials, 'app_id', ''))
        app_secret = normalize(getattr(credentials, 'app_secret', ''))
        if not app_id or not app_secret:
            raise PurchaseAssistantError(
                '小犀代采飞书企业应用凭证尚未配置')
        response = self.transport.request_json(
            'POST', self.config.api_base
            + '/auth/v3/tenant_access_token/internal',
            payload={'app_id': app_id, 'app_secret': app_secret})
        credentials = None
        app_secret = ''
        if response.get('code') != 0:
            raise PurchaseAssistantError('飞书应用凭证校验失败')
        token = normalize(response.get('tenant_access_token'))
        try:
            expires_in = int(response.get('expire') or 0)
        except (TypeError, ValueError):
            expires_in = 0
        if not token or expires_in <= 0:
            raise PurchaseAssistantError('飞书访问令牌返回异常')
        self._token = token
        self._token_expires_at = now + max(1, expires_in - 120)
        return token

    def _fetch_values(self, retry_auth=True):
        expression = '%s!%s' % (
            self.config.sheet_id, self.config.cell_range)
        url = (self.config.api_base + '/sheets/v2/spreadsheets/'
               + quote(self.config.spreadsheet_token, safe='') + '/values/'
               + quote(expression, safe=''))
        try:
            response = self.transport.request_json(
                'GET', url,
                headers={'Authorization': 'Bearer ' + self._get_token()})
        except TokenRejectedError:
            if not retry_auth:
                raise PurchaseAssistantError('飞书应用无权读取采购协作表')
            self._invalidate_token()
            return self._fetch_values(retry_auth=False)
        code = response.get('code')
        if code != 0:
            if code in {99991661, 99991663, 99991668} and retry_auth:
                self._invalidate_token()
                return self._fetch_values(retry_auth=False)
            if code in {91403, 99991672}:
                raise PurchaseAssistantError(
                    '飞书应用无表格读取权限，请检查 API 权限和文档共享范围')
            raise PurchaseAssistantError(
                '飞书表格 API 返回错误（code=%s）' % code)
        data = response.get('data') or {}
        value_range = data.get('valueRange') or data.get('value_range') or {}
        values = value_range.get('values')
        if not isinstance(values, list):
            raise PurchaseAssistantError('飞书表格 API 未返回单元格数据')
        return values

    def _read_rows(self):
        with self._lock:
            now = time.monotonic()
            if (self._cached_rows is not None
                    and now - self._cached_at
                    <= self.config.cache_ttl_seconds):
                return self._cached_rows
            rows = rows_from_values(self._fetch_values())
            self._cached_rows = rows
            self._cached_at = now
            return rows

    def list_tasks(self):
        return rows_to_tasks(self._read_rows())

    def get_recipient(self, key):
        return find_recipient(self._read_rows(), key)


class PurchaseAssistantService(object):
    def __init__(self, provider=None, config_error=''):
        self.provider = provider
        self.config_error = str(config_error or '')
        self.session_token = secrets.token_urlsafe(32)

    @classmethod
    def from_runtime_config(cls, mapping, credential_getter):
        try:
            config = PurchaseAssistantConfig.from_runtime_config(mapping)
            return cls(PurchaseAssistantSheetProvider(
                config, credential_getter))
        except (PurchaseAssistantError, TypeError, ValueError) as exc:
            return cls(config_error=str(exc))

    @property
    def configured(self):
        return self.provider is not None

    def issue_session(self):
        return self.session_token

    def authorize(self, authorization):
        expected = 'Bearer ' + self.session_token
        return bool(self.session_token) and secrets.compare_digest(
            str(authorization or ''), expected)

    def search(self, query, limit=20):
        if not self.provider:
            raise PurchaseAssistantError(
                self.config_error or '采购助手数据源尚未配置')
        return search_tasks(self.provider.list_tasks(), query, limit)

    def recipient(self, key):
        if not self.provider:
            raise PurchaseAssistantError(
                self.config_error or '采购助手数据源尚未配置')
        return self.provider.get_recipient(key)
