# -*- coding: utf-8 -*-
"""Resource Center read/reconcile layer and local proxy health checks.

Phase P0/P1 is intentionally read-only for Feishu and HubStudio.  Shop and
proxy credentials are excluded from normal reads.  Proxy credentials are
loaded only for an explicitly started local check job and are never included
in snapshots, exports, logs, or retained history.
"""
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
import base64
import csv
from datetime import datetime, timezone
import hashlib
from io import StringIO
import ipaddress
import json
import os
import socket
import ssl
import threading
import time

from .lark_openapi import LarkOpenApiClient
from .redaction import scrub_text


DEFAULT_STORE_BASE_TOKEN = 'PUcbbY5LqafK83s7GXYcufjGn4f'
DEFAULT_STORE_TABLE_ID = 'tblsE8O29ltTsxTR'
DEFAULT_PROXY_SPREADSHEET_TOKEN = 'MCRTsuxVBhq0iBtMWJKc1TAon9g'
DEFAULT_PROXY_SHEET_ID = 'a82543'
DEFAULT_PROXY_ASSET_BASE_TOKEN = 'XZ0sbXEhAarPqwsekGYcubB0nrd'
DEFAULT_PROXY_ASSET_TABLE_ID = 'tblc8YoeTEhkjci8'

SHOP_SAFE_FIELDS = (
    '店铺ID', '店铺中文名', '平台', '站点', '主管负责人', '运营人员',
    '店铺状况', '店铺运营状态', '开店状态', '店铺属性',
)
PROXY_METADATA_FIELDS = (
    'IP地址', '端口', '协议', '国家代码', '资产状态', 'Webshare有效',
    '最近验证时间',
)

# Business taxonomy is deliberately independent from the provider name.  The
# catalog describes all confirmed proxy types even when a type is dynamic (and
# therefore has no fixed inventory rows) or has not been put into use yet.
PROXY_TYPE_DEFINITIONS = (
    {
        'code': 'dynamic_residential',
        'label': '动态住宅代理',
        'provider': '711',
        'usage_scenario_code': 'procurement',
        'usage_scenario': '采购场景',
        'acquisition_mode_code': 'api_dynamic',
        'acquisition_mode': 'API 动态提取',
        'access_requirement': '711 白名单需添加当前执行器所在网络的出口 IP',
        'usage_status': '采购使用中',
        'inventory_mode': 'dynamic',
    },
    {
        'code': 'static_datacenter',
        'label': '静态数据中心 IP',
        'provider': 'Webshare',
        'usage_scenario_code': 'store_environment',
        'usage_scenario': '绑定店铺环境',
        'acquisition_mode_code': 'static_inventory',
        'acquisition_mode': '静态资产台账',
        'access_requirement': '按资产凭证连接',
        'usage_status': '店铺环境使用中',
        'inventory_mode': 'fixed',
    },
    {
        'code': 'static_residential',
        'label': '静态住宅 IP',
        'provider': '待配置',
        'usage_scenario_code': 'none',
        'usage_scenario': '暂无使用场景',
        'acquisition_mode_code': 'not_connected',
        'acquisition_mode': '尚未接入',
        'access_requirement': '待确认',
        'usage_status': '未启用',
        'inventory_mode': 'not_connected',
    },
)
PROXY_TYPE_BY_CODE = {
    item['code']: item for item in PROXY_TYPE_DEFINITIONS
}

PROXY_CHECK_HOST = 'api.ipify.org'
PROXY_CHECK_PATH = '/?format=json'
PROXY_CHECK_PORT = 443
MAX_PROXY_CHECK_ITEMS = 200


def _text(value):
    """Normalize Feishu scalar/select/rich-text values to safe display text."""
    if value is None:
        return ''
    if isinstance(value, dict):
        return str(value.get('name') or value.get('text') or value.get('value') or '')
    if isinstance(value, list):
        return '、'.join(filter(None, (_text(item) for item in value)))
    return str(value).strip()


def _normalized_name(value):
    return ''.join(str(value or '').strip().casefold().split())


def _proxy_type_code(proxy):
    explicit = str(
        proxy.get('proxy_type_code') or proxy.get('proxy_type') or ''
    ).strip().casefold()
    explicit_compact = explicit.replace('_', '').replace('-', '').replace(' ', '')
    aliases = {
        'dynamicresidential': 'dynamic_residential',
        '动态住宅代理': 'dynamic_residential',
        '动态住宅ip': 'dynamic_residential',
        'staticdatacenter': 'static_datacenter',
        '静态数据中心ip': 'static_datacenter',
        '静态机房ip': 'static_datacenter',
        'staticresidential': 'static_residential',
        '静态住宅ip': 'static_residential',
        '静态住宅代理': 'static_residential',
    }
    if explicit in PROXY_TYPE_BY_CODE:
        return explicit
    if explicit_compact in aliases:
        return aliases[explicit_compact]

    provider = _normalized_name(proxy.get('source') or proxy.get('provider'))
    if '711' in provider:
        return 'dynamic_residential'
    if 'webshare' in provider:
        return 'static_datacenter'
    if '静态住宅' in provider:
        return 'static_residential'
    return 'unclassified'


def _proxy_classification(proxy):
    code = _proxy_type_code(proxy)
    definition = PROXY_TYPE_BY_CODE.get(code)
    if definition is None:
        return {
            'code': 'unclassified',
            'label': '类型待确认',
            'usage_scenario_code': 'pending',
            'usage_scenario': '使用场景待确认',
            'acquisition_mode_code': 'pending',
            'acquisition_mode': '获取方式待确认',
            'access_requirement': '待确认',
            'health_check_supported': False,
        }
    classified = dict(definition)
    # P1 health checks load credentials from the Webshare fixed-asset ledger.
    # Dynamic 711 extraction and not-yet-connected proxy types must not enter
    # that credential-loading path.
    classified['health_check_supported'] = (
        code == 'static_datacenter'
        and 'webshare' in _normalized_name(
            proxy.get('source') or proxy.get('provider'))
    )
    return classified


def _stable_id(prefix, *parts):
    raw = '|'.join(str(part or '').strip().casefold() for part in parts)
    return '%s_%s' % (prefix, hashlib.sha256(raw.encode('utf-8')).hexdigest()[:16])


def _mask_identifier(value):
    value = str(value or '').strip()
    if not value:
        return ''
    if len(value) <= 4:
        return '***'
    return '***' + value[-4:]


def mask_ip(value):
    value = str(value or '').strip()
    if not value:
        return ''
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        if len(value) <= 6:
            return '***'
        return value[:2] + '***' + value[-2:]
    if address.version == 4:
        parts = value.split('.')
        return '%s.***.***.%s' % (parts[0], parts[-1])
    return address.exploded.split(':')[0] + ':****:****:' + address.exploded.split(':')[-1]


def _mask_port(value):
    value = str(value or '').strip()
    if not value:
        return ''
    return '***' + value[-2:]


def _parse_port(value):
    try:
        port = int(float(str(value or '').strip()))
    except (TypeError, ValueError):
        return 0
    return port if 1 <= port <= 65535 else 0


def _shop_site_code(value):
    value = _text(value)
    mappings = {
        '墨西哥': 'MX', '墨西哥站': 'MX', '美国站': 'US', '美国': 'US',
    }
    return mappings.get(value, value)


def _hub_group(env):
    value = env.get('tagName')
    if not value:
        value = env.get('tagNames')
    return _text(value)


def _is_hub_browser(value):
    value = _normalized_name(value)
    return value in ('hubstudio', 'hub') or 'hubstudio' in value


def _active_proxy_row(row):
    if not row.get('shop_name'):
        return False
    status = _normalized_name(row.get('asset_status'))
    return not any(term in status for term in ('已更换', '有问题', '已作废', '异常', '替换'))


@dataclass(frozen=True)
class ResourceSourceConfig:
    store_base_token: str = DEFAULT_STORE_BASE_TOKEN
    store_table_id: str = DEFAULT_STORE_TABLE_ID
    proxy_spreadsheet_token: str = DEFAULT_PROXY_SPREADSHEET_TOKEN
    proxy_sheet_id: str = DEFAULT_PROXY_SHEET_ID
    proxy_asset_base_token: str = DEFAULT_PROXY_ASSET_BASE_TOKEN
    proxy_asset_table_id: str = DEFAULT_PROXY_ASSET_TABLE_ID

    @classmethod
    def from_environment(cls):
        return cls(
            store_base_token=os.environ.get(
                'XYNIGO_STORE_BASE_TOKEN', DEFAULT_STORE_BASE_TOKEN),
            store_table_id=os.environ.get(
                'XYNIGO_STORE_TABLE_ID', DEFAULT_STORE_TABLE_ID),
            proxy_spreadsheet_token=os.environ.get(
                'XYNIGO_PROXY_SPREADSHEET_TOKEN',
                DEFAULT_PROXY_SPREADSHEET_TOKEN),
            proxy_sheet_id=os.environ.get(
                'XYNIGO_PROXY_SHEET_ID', DEFAULT_PROXY_SHEET_ID),
            proxy_asset_base_token=os.environ.get(
                'XYNIGO_PROXY_ASSET_BASE_TOKEN',
                DEFAULT_PROXY_ASSET_BASE_TOKEN),
            proxy_asset_table_id=os.environ.get(
                'XYNIGO_PROXY_ASSET_TABLE_ID',
                DEFAULT_PROXY_ASSET_TABLE_ID),
        )


@dataclass(frozen=True)
class ProxyEndpoint:
    asset_id: str
    host: str
    port: int
    protocol: str
    username: str = ''
    password: str = ''

    @property
    def endpoint_key(self):
        return '%s|%s|%s' % (
            self.protocol.casefold(), self.host.casefold(), self.port)


class FeishuResourceReader(object):
    """Read only the columns needed by Resource Center."""

    def __init__(self, credential_provider, config=None, client_factory=LarkOpenApiClient):
        self.credential_provider = credential_provider
        self.config = config or ResourceSourceConfig.from_environment()
        self.client_factory = client_factory

    def _client(self, base_token='', table_id=''):
        return self.client_factory(
            credential_provider=self.credential_provider,
            base_token=base_token, table_id=table_id)

    def list_shops(self):
        client = self._client(
            self.config.store_base_token, self.config.store_table_id)
        records = client.list_records(field_names=SHOP_SAFE_FIELDS)
        result = []
        for record in records:
            fields = record.get('fields') or {}
            name = _text(fields.get('店铺中文名'))
            result.append({
                'shop_id': _stable_id(
                    'shop', record.get('record_id') or name,
                    _text(fields.get('店铺ID'))),
                'shop_name': name,
                'platform': _text(fields.get('平台')),
                'site': _shop_site_code(fields.get('站点')),
                'owner': _text(fields.get('运营人员')) or _text(
                    fields.get('主管负责人')),
                'manager': _text(fields.get('主管负责人')),
                'shop_status': _text(fields.get('店铺运营状态')) or _text(
                    fields.get('店铺状况')) or _text(fields.get('开店状态')),
                'shop_type': _text(fields.get('店铺属性')),
            })
        return result

    def _sheet_values(self, range_a1):
        client = self._client()
        target = '%s!%s' % (self.config.proxy_sheet_id, range_a1)
        return client.get_spreadsheet_values(
            self.config.proxy_spreadsheet_token, target)

    def list_proxy_rows(self):
        # Deliberately skip C:D (proxy username/password).
        ab = self._sheet_values('A1:B5000')
        ej = self._sheet_values('E1:J5000')
        lr = self._sheet_values('L1:R5000')
        total = max(len(ab), len(ej), len(lr))
        result = []
        for index in range(1, total):
            left = ab[index] if index < len(ab) else []
            middle = ej[index] if index < len(ej) else []
            right = lr[index] if index < len(lr) else []
            host = _text(left[0] if len(left) > 0 else '')
            port = _parse_port(left[1] if len(left) > 1 else '')
            if not host or not port:
                continue
            def middle_at(offset):
                return _text(middle[offset] if len(middle) > offset else '')
            def right_at(offset):
                return _text(right[offset] if len(right) > offset else '')
            source = right_at(2) or 'Webshare'  # N 代理来源
            result.append({
                'asset_id': _stable_id('ip', source, host, port),
                'host': host,
                'port': port,
                'other_name': middle_at(0),
                'shop_name': middle_at(1),
                'department': middle_at(2),
                'shop_type': middle_at(3),
                'platform': middle_at(4),
                'browser': middle_at(5),
                'env_serial': right_at(1),  # M 窗口序号
                'occupancy_known': True,
                'source': source,
                'asset_status': right_at(3) or '待确认',
                'record_source': right_at(4),
                'recorded_at': right_at(5),
                'remark': right_at(6),
            })
        return result

    def list_proxy_metadata(self):
        client = self._client(
            self.config.proxy_asset_base_token,
            self.config.proxy_asset_table_id)
        records = client.list_records(field_names=PROXY_METADATA_FIELDS)
        result = {}
        for record in records:
            fields = record.get('fields') or {}
            host = _text(fields.get('IP地址'))
            port = _parse_port(fields.get('端口'))
            if not host or not port:
                continue
            result[(host, port)] = {
                'protocol': _text(fields.get('协议')) or 'SOCKS5',
                'country': _text(fields.get('国家代码')),
                'provider_status': _text(fields.get('资产状态')),
                'provider_valid': bool(fields.get('Webshare有效')),
                'provider_checked_at': _text(fields.get('最近验证时间')),
            }
        return result

    def load_proxy_endpoints(self, asset_ids, protocol_by_asset=None):
        """Load credentials only for an explicit check; caller must discard."""
        requested = set(asset_ids or [])
        if not requested:
            raise ValueError('至少选择一个代理 IP')
        values = self._sheet_values('A2:D5000')
        endpoints = []
        for row in values:
            host = _text(row[0] if len(row) > 0 else '')
            port = _parse_port(row[1] if len(row) > 1 else '')
            if not host or not port:
                continue
            asset_id = _stable_id('ip', 'Webshare', host, port)
            if asset_id not in requested:
                continue
            endpoints.append(ProxyEndpoint(
                asset_id=asset_id,
                host=host,
                port=port,
                protocol=str((protocol_by_asset or {}).get(
                    asset_id) or 'SOCKS5'),
                username=_text(row[2] if len(row) > 2 else ''),
                password=_text(row[3] if len(row) > 3 else ''),
            ))
        found = {endpoint.asset_id for endpoint in endpoints}
        missing = requested - found
        if missing:
            raise ValueError('所选代理 IP 已变化，请刷新列表后重试')
        return endpoints


def _read_until(sock, marker, limit=16384):
    data = b''
    while marker not in data and len(data) < limit:
        chunk = sock.recv(min(4096, limit - len(data)))
        if not chunk:
            break
        data += chunk
    return data


def _socks5_connect(endpoint, target_host, target_port, timeout):
    sock = socket.create_connection((endpoint.host, endpoint.port), timeout)
    sock.settimeout(timeout)
    methods = b'\x00\x02' if endpoint.username else b'\x00'
    sock.sendall(b'\x05' + bytes([len(methods)]) + methods)
    reply = sock.recv(2)
    if len(reply) != 2 or reply[0] != 5 or reply[1] == 0xff:
        sock.close()
        raise ProxyCheckError('proxy_auth_failed', '代理认证方式不受支持')
    if reply[1] == 2:
        username = endpoint.username.encode('utf-8')
        password = endpoint.password.encode('utf-8')
        if len(username) > 255 or len(password) > 255:
            sock.close()
            raise ProxyCheckError('proxy_auth_failed', '代理认证失败')
        sock.sendall(
            b'\x01' + bytes([len(username)]) + username
            + bytes([len(password)]) + password)
        auth_reply = sock.recv(2)
        if len(auth_reply) != 2 or auth_reply[1] != 0:
            sock.close()
            raise ProxyCheckError('proxy_auth_failed', '代理认证失败')
    encoded_host = target_host.encode('idna')
    request = (
        b'\x05\x01\x00\x03' + bytes([len(encoded_host)]) + encoded_host
        + int(target_port).to_bytes(2, 'big'))
    sock.sendall(request)
    reply = sock.recv(4)
    if len(reply) != 4 or reply[0] != 5 or reply[1] != 0:
        sock.close()
        raise ProxyCheckError('proxy_connect_failed', '代理无法访问固定检测地址')
    atyp = reply[3]
    if atyp == 1:
        address_len = 4
    elif atyp == 4:
        address_len = 16
    elif atyp == 3:
        length = sock.recv(1)
        address_len = length[0] if length else 0
    else:
        sock.close()
        raise ProxyCheckError('proxy_protocol_error', '代理协议响应无效')
    remaining = address_len + 2
    while remaining:
        chunk = sock.recv(remaining)
        if not chunk:
            sock.close()
            raise ProxyCheckError('proxy_protocol_error', '代理协议响应不完整')
        remaining -= len(chunk)
    return sock


def _http_proxy_connect(endpoint, target_host, target_port, timeout):
    sock = socket.create_connection((endpoint.host, endpoint.port), timeout)
    sock.settimeout(timeout)
    headers = [
        'CONNECT %s:%s HTTP/1.1' % (target_host, target_port),
        'Host: %s:%s' % (target_host, target_port),
        'Connection: close',
    ]
    if endpoint.username:
        token = base64.b64encode(
            ('%s:%s' % (endpoint.username, endpoint.password)).encode('utf-8')
        ).decode('ascii')
        headers.append('Proxy-Authorization: Basic ' + token)
    sock.sendall(('\r\n'.join(headers) + '\r\n\r\n').encode('ascii'))
    response = _read_until(sock, b'\r\n\r\n')
    status_line = response.split(b'\r\n', 1)[0]
    if b' 407 ' in status_line:
        sock.close()
        raise ProxyCheckError('proxy_auth_failed', '代理认证失败')
    if b' 200 ' not in status_line:
        sock.close()
        raise ProxyCheckError('proxy_connect_failed', '代理无法访问固定检测地址')
    return sock


def _https_exit_ip(sock, timeout):
    context = ssl.create_default_context()
    wrapped = context.wrap_socket(sock, server_hostname=PROXY_CHECK_HOST)
    wrapped.settimeout(timeout)
    request = (
        'GET %s HTTP/1.1\r\nHost: %s\r\nAccept: application/json\r\n'
        'Connection: close\r\nUser-Agent: Xynigo-Proxy-Check/1\r\n\r\n'
    ) % (PROXY_CHECK_PATH, PROXY_CHECK_HOST)
    wrapped.sendall(request.encode('ascii'))
    response = b''
    while len(response) < 65536:
        chunk = wrapped.recv(4096)
        if not chunk:
            break
        response += chunk
    wrapped.close()
    head, separator, body = response.partition(b'\r\n\r\n')
    if not separator or b' 200 ' not in head.split(b'\r\n', 1)[0]:
        raise ProxyCheckError('check_target_failed', '固定检测地址返回异常')
    try:
        payload = json.loads(body.decode('utf-8'))
        exit_ip = str(payload.get('ip') or '').strip()
    except Exception:
        exit_ip = body.decode('utf-8', errors='ignore').strip()
    try:
        ipaddress.ip_address(exit_ip)
    except ValueError as exc:
        raise ProxyCheckError('check_target_failed', '固定检测地址未返回有效出口 IP') from exc
    return exit_ip


class ProxyCheckError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


class ProxyNetworkChecker(object):
    def check(self, endpoint, timeout=8.0):
        started = time.monotonic()
        try:
            protocol = str(endpoint.protocol or 'SOCKS5').strip().casefold()
            if protocol in ('socks5', 'socks'):
                sock = _socks5_connect(
                    endpoint, PROXY_CHECK_HOST, PROXY_CHECK_PORT, timeout)
            elif protocol in ('http', 'https'):
                sock = _http_proxy_connect(
                    endpoint, PROXY_CHECK_HOST, PROXY_CHECK_PORT, timeout)
            else:
                raise ProxyCheckError('protocol_unsupported', '暂不支持该代理协议')
            exit_ip = _https_exit_ip(sock, timeout)
            try:
                host_ip = ipaddress.ip_address(endpoint.host)
            except ValueError:
                host_ip = None
            if host_ip is not None and host_ip != ipaddress.ip_address(exit_ip):
                raise ProxyCheckError('exit_ip_mismatch', '出口 IP 与台账资产不一致')
            return {
                'ok': True,
                'code': 'ok',
                'message': '代理可用，出口 IP 一致',
                'latencyMs': int((time.monotonic() - started) * 1000),
                'exitIpMasked': mask_ip(exit_ip),
            }
        except ProxyCheckError:
            raise
        except (socket.timeout, TimeoutError) as exc:
            raise ProxyCheckError(
                'network_or_whitelist', '连接超时，请同时检查本机网络与供应商白名单') from exc
        except ConnectionRefusedError as exc:
            raise ProxyCheckError('proxy_unreachable', '代理端口拒绝连接') from exc
        except (OSError, ssl.SSLError) as exc:
            raise ProxyCheckError(
                'network_or_whitelist', '无法完成代理链路，请检查本机网络或供应商白名单') from exc


class ProxyCheckJob(object):
    """In-memory check runner; only redacted results survive task completion."""

    def __init__(self, endpoint_loader, checker=None, clock=time.time):
        self.endpoint_loader = endpoint_loader
        self.checker = checker or ProxyNetworkChecker()
        self.clock = clock
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.running = False
        self.started_at = None
        self.finished_at = None
        self.rows = []
        self.history_by_asset = {}

    def _public_result(self, endpoint, result, attempts, conflict=False):
        return {
            'assetId': endpoint.asset_id,
            'state': 'ok' if result.get('ok') else 'failed',
            'checkStatus': '正常' if result.get('ok') else '异常',
            'code': result.get('code') or 'check_failed',
            'message': scrub_text(result.get('message') or '检测失败')[:160],
            'latencyMs': result.get('latencyMs'),
            'exitIpMasked': result.get('exitIpMasked') or '',
            'attempts': attempts,
            'conflict': bool(conflict),
            'checkedAt': datetime.now(timezone.utc).isoformat(timespec='seconds'),
        }

    def _check_with_retry(self, endpoint, timeout):
        last = None
        for attempt in (1, 2):
            if self.stop_event.is_set():
                return self._public_result(endpoint, {
                    'ok': False, 'code': 'stopped', 'message': '任务已停止',
                }, attempt - 1)
            try:
                result = self.checker.check(endpoint, timeout=timeout)
                return self._public_result(endpoint, result, attempt)
            except ProxyCheckError as exc:
                last = exc
            except Exception:
                last = ProxyCheckError('check_failed', '代理检测失败')
        return self._public_result(endpoint, {
            'ok': False,
            'code': getattr(last, 'code', 'check_failed'),
            'message': str(last or '代理检测失败'),
        }, 2)

    def start(self, asset_ids, concurrency=10, timeout=8):
        asset_ids = list(dict.fromkeys(str(item or '').strip()
                                       for item in (asset_ids or []) if item))
        if not asset_ids:
            raise ValueError('至少选择一个代理 IP')
        if len(asset_ids) > MAX_PROXY_CHECK_ITEMS:
            raise ValueError('单批最多检测 %s 个代理 IP' % MAX_PROXY_CHECK_ITEMS)
        try:
            concurrency = int(concurrency)
            timeout = float(timeout)
        except (TypeError, ValueError) as exc:
            raise ValueError('并发数或超时设置无效') from exc
        if not 1 <= concurrency <= 20:
            raise ValueError('并发数必须在 1-20 之间')
        if not 3 <= timeout <= 30:
            raise ValueError('超时必须在 3-30 秒之间')
        with self.lock:
            if self.running:
                raise RuntimeError('已有代理检测任务在进行')
        endpoints = self.endpoint_loader(asset_ids)
        with self.lock:
            if self.running:
                raise RuntimeError('已有代理检测任务在进行')
            self.running = True
            self.started_at = self.clock()
            self.finished_at = None
            self.stop_event.clear()
            self.rows = [{
                'assetId': endpoint.asset_id, 'state': 'pending',
                'checkStatus': '待检测', 'message': '', 'attempts': 0,
            } for endpoint in endpoints]

        def worker():
            # Same endpoint is checked once. Duplicate ledger rows receive the
            # same redacted result and an explicit conflict flag.
            grouped = {}
            for endpoint in endpoints:
                grouped.setdefault(endpoint.endpoint_key, []).append(endpoint)
            results = {}
            try:
                with ThreadPoolExecutor(max_workers=concurrency) as pool:
                    futures = {
                        pool.submit(self._check_with_retry, group[0], timeout): key
                        for key, group in grouped.items()
                    }
                    for future in as_completed(futures):
                        key = futures[future]
                        primary_result = future.result()
                        group = grouped[key]
                        for endpoint in group:
                            row = dict(primary_result)
                            row['assetId'] = endpoint.asset_id
                            row['conflict'] = len(group) > 1
                            results[endpoint.asset_id] = row
                        with self.lock:
                            self.rows = [
                                dict(results.get(endpoint.asset_id, {
                                    'assetId': endpoint.asset_id,
                                    'state': 'running',
                                    'checkStatus': '检测中',
                                    'message': '', 'attempts': 0,
                                })) for endpoint in endpoints
                            ]
                with self.lock:
                    self.rows = [dict(results[endpoint.asset_id])
                                 for endpoint in endpoints]
                    for row in self.rows:
                        history = self.history_by_asset.setdefault(row['assetId'], [])
                        history.insert(0, dict(row))
                        del history[20:]
            finally:
                # Drop the only long-lived reference to credential-bearing
                # ProxyEndpoint objects when the worker exits.
                endpoints.clear()
                with self.lock:
                    self.finished_at = self.clock()
                    self.running = False

        threading.Thread(target=worker, daemon=True).start()
        return len(asset_ids)

    def stop(self):
        self.stop_event.set()

    def snapshot(self):
        with self.lock:
            end = self.clock() if self.running else self.finished_at
            elapsed = int(max(0, end - self.started_at)) \
                if self.started_at and end else 0
            done = sum(row.get('state') in ('ok', 'failed') for row in self.rows)
            return {
                'running': self.running,
                'elapsedSec': elapsed,
                'total': len(self.rows),
                'done': done,
                'normal': sum(row.get('state') == 'ok' for row in self.rows),
                'abnormal': sum(row.get('state') == 'failed' for row in self.rows),
                'rows': [dict(row) for row in self.rows],
            }

    def latest(self, asset_id):
        with self.lock:
            rows = self.history_by_asset.get(asset_id) or []
            return dict(rows[0]) if rows else None

    def history(self, asset_id):
        with self.lock:
            return [dict(row) for row in self.history_by_asset.get(asset_id) or []]


class ResourceCenterService(object):
    def __init__(self, hub_getter, credential_provider, reader=None,
                 checker=None, cache_ttl=20.0, clock=time.time):
        self.hub_getter = hub_getter
        self.reader = reader or FeishuResourceReader(credential_provider)
        self.cache_ttl = float(cache_ttl)
        self.clock = clock
        self.lock = threading.Lock()
        self.cached_at = 0.0
        self.cached = None
        self.check_job = ProxyCheckJob(
            self._load_selected_endpoints, checker=checker, clock=clock)

    def invalidate(self):
        with self.lock:
            self.cached = None
            self.cached_at = 0.0

    @staticmethod
    def _source_state(ready, label, error=''):
        return {
            'ready': bool(ready), 'label': label,
            'error': scrub_text(error)[:180] if error else '',
        }

    def _refresh(self):
        sources = {}
        shops = []
        proxies = []
        metadata = {}
        hub_envs = []
        open_codes = set()
        try:
            shops = self.reader.list_shops()
            sources['stores'] = self._source_state(
                True, '飞书 Base · 跨境店铺信息登记表')
        except Exception as exc:
            sources['stores'] = self._source_state(
                False, '飞书 Base · 跨境店铺信息登记表', exc)
        try:
            proxies = self.reader.list_proxy_rows()
            sources['proxyLedger'] = self._source_state(
                True, '飞书表格 · Webshare IP 总表')
        except Exception as exc:
            sources['proxyLedger'] = self._source_state(
                False, '飞书表格 · Webshare IP 总表', exc)
        try:
            metadata = self.reader.list_proxy_metadata()
            sources['proxyProvider'] = self._source_state(
                True, '飞书 Base · Webshare 资产同步数据')
        except Exception as exc:
            sources['proxyProvider'] = self._source_state(
                False, '飞书 Base · Webshare 资产同步数据', exc)
        if not proxies and metadata:
            # Keep the proxy page useful when the ordinary spreadsheet has
            # not yet granted the enterprise app Sheets scope/document ACL.
            # Occupancy and credentials remain unavailable and are never
            # guessed from the provider-only Base.
            for (host, port), extra in metadata.items():
                proxies.append({
                    'asset_id': _stable_id('ip', 'Webshare', host, port),
                    'host': host,
                    'port': port,
                    'shop_name': '',
                    'department': '',
                    'shop_type': '',
                    'platform': '',
                    'browser': '',
                    'env_serial': '',
                    'occupancy_known': False,
                    'source': 'Webshare',
                    'asset_status': extra.get('provider_status') or '待确认',
                    'remark': '',
                })
        try:
            hub = self.hub_getter()
            hub_envs = hub.env_list()
            try:
                open_codes = hub.open_container_codes()
            except Exception:
                open_codes = set()
            sources['hub'] = self._source_state(
                True, 'HubStudio Local API · 全量环境只读')
        except Exception as exc:
            sources['hub'] = self._source_state(
                False, 'HubStudio Local API · 全量环境只读', exc)

        for proxy in proxies:
            extra = metadata.get((proxy['host'], proxy['port'])) or {}
            proxy.update(extra)
            proxy.setdefault('protocol', 'SOCKS5')
            proxy.setdefault('country', '')

        return self._reconcile(shops, proxies, hub_envs, open_codes, sources)

    def _snapshot(self, force=False):
        with self.lock:
            if (not force and self.cached is not None
                    and self.clock() - self.cached_at < self.cache_ttl):
                return self.cached
        refreshed = self._refresh()
        with self.lock:
            self.cached = refreshed
            self.cached_at = self.clock()
            return refreshed

    def _reconcile(self, shops, proxies, hub_envs, open_codes, sources):
        env_by_serial = {
            str(env.get('serialNumber') or '').strip(): env
            for env in hub_envs if str(env.get('serialNumber') or '').strip()
        }
        env_name_index = {}
        for env in hub_envs:
            key = _normalized_name(env.get('containerName'))
            if key:
                env_name_index.setdefault(key, []).append(env)

        proxy_by_shop = {}
        endpoint_occupancy = {}
        for proxy in proxies:
            shop_key = _normalized_name(proxy.get('shop_name'))
            if shop_key:
                proxy_by_shop.setdefault(shop_key, []).append(proxy)
            if _active_proxy_row(proxy):
                endpoint_occupancy.setdefault(
                    (proxy['host'], proxy['port']), set()).add(shop_key)

        shop_name_counts = {}
        for shop in shops:
            key = _normalized_name(shop.get('shop_name'))
            if key:
                shop_name_counts[key] = shop_name_counts.get(key, 0) + 1

        public_stores = []
        bound_serial_to_shops = {}
        for shop in shops:
            shop_key = _normalized_name(shop.get('shop_name'))
            allocations = proxy_by_shop.get(shop_key, [])
            active_allocations = [row for row in allocations if _active_proxy_row(row)]
            explicit_envs = []
            for allocation in active_allocations:
                serial = str(allocation.get('env_serial') or '').strip()
                if serial and _is_hub_browser(allocation.get('browser')):
                    env = env_by_serial.get(serial)
                    if env and env not in explicit_envs:
                        explicit_envs.append(env)
            candidates = env_name_index.get(shop_key, []) if shop_key else []
            mapping_state = 'unbound'
            env = None
            if len(explicit_envs) == 1:
                env = explicit_envs[0]
                mapping_state = 'bound'
            elif not explicit_envs and len(candidates) == 1:
                env = candidates[0]
                mapping_state = 'candidate'
            if env:
                serial = str(env.get('serialNumber') or '')
                bound_serial_to_shops.setdefault(serial, []).append(shop['shop_id'])
            proxy = active_allocations[0] if active_allocations else (
                allocations[0] if allocations else None)
            conflicts = []
            if shop_name_counts.get(shop_key, 0) > 1:
                conflicts.append('店铺中文名重复')
            if len(explicit_envs) > 1:
                conflicts.append('同一店铺绑定多个 Hub 环境')
            if len(active_allocations) > 1:
                conflicts.append('同一店铺存在多个当前代理')
            if proxy and len(endpoint_occupancy.get(
                    (proxy['host'], proxy['port']), set())) > 1:
                conflicts.append('代理 IP 被多个店铺占用')
            container_code = str((env or {}).get('containerCode') or '')
            public_stores.append({
                'shopId': shop['shop_id'],
                'shopName': shop.get('shop_name') or '未命名店铺',
                'platform': shop.get('platform') or '',
                'site': shop.get('site') or '',
                'department': (proxy or {}).get('department') or '',
                'owner': shop.get('owner') or '',
                'manager': shop.get('manager') or '',
                'shopStatus': shop.get('shop_status') or '待确认',
                'shopType': shop.get('shop_type') or (
                    (proxy or {}).get('shop_type') or ''),
                'mappingState': mapping_state,
                'hubGroup': _hub_group(env or {}),
                'envName': str((env or {}).get('containerName') or ''),
                'envSerial': str((env or {}).get('serialNumber') or ''),
                'containerCodeMasked': _mask_identifier(container_code),
                'hubRuntimeStatus': (
                    '运行中' if container_code and container_code in open_codes
                    else ('已关闭' if env else '未绑定')),
                'proxyAssetId': (proxy or {}).get('asset_id') or '',
                'proxyAddressMasked': (
                    '%s:%s' % (mask_ip(proxy['host']), _mask_port(proxy['port']))
                    if proxy else ''),
                'proxySource': (proxy or {}).get('source') or '',
                'proxyStatus': (proxy or {}).get('asset_status') or '未分配',
                'conflicts': conflicts,
            })

        for row in public_stores:
            serial = row.get('envSerial')
            if serial and len(bound_serial_to_shops.get(serial, [])) > 1:
                if 'Hub 环境被多个店铺占用' not in row['conflicts']:
                    row['conflicts'].append('Hub 环境被多个店铺占用')

        public_proxies = []
        for proxy in proxies:
            shops_for_endpoint = endpoint_occupancy.get(
                (proxy['host'], proxy['port']), set())
            conflict = len(shops_for_endpoint) > 1
            latest = self.check_job.latest(proxy['asset_id'])
            classification = _proxy_classification(proxy)
            public_proxies.append({
                'assetId': proxy['asset_id'],
                'addressMasked': '%s:%s' % (
                    mask_ip(proxy['host']), _mask_port(proxy['port'])),
                'provider': proxy.get('source') or 'Webshare',
                'proxyTypeCode': classification['code'],
                'proxyType': classification['label'],
                'usageScenarioCode': classification['usage_scenario_code'],
                'usageScenario': classification['usage_scenario'],
                'acquisitionModeCode': classification['acquisition_mode_code'],
                'acquisitionMode': classification['acquisition_mode'],
                'accessRequirement': classification['access_requirement'],
                'healthCheckSupported': classification[
                    'health_check_supported'],
                'country': proxy.get('country') or '',
                'protocol': proxy.get('protocol') or 'SOCKS5',
                'assetStatus': proxy.get('asset_status') or '待确认',
                'providerStatus': proxy.get('provider_status') or '',
                'providerValid': proxy.get('provider_valid'),
                'checkStatus': (latest or {}).get('checkStatus') or '待检测',
                'occupiedShop': proxy.get('shop_name') or '',
                'occupancyKnown': proxy.get('occupancy_known', True),
                'department': proxy.get('department') or '',
                'browser': proxy.get('browser') or '',
                'envSerial': proxy.get('env_serial') or '',
                'lastCheckDevice': '当前电脑' if latest else '',
                'lastCheckAt': (latest or {}).get('checkedAt') or '',
                'latencyMs': (latest or {}).get('latencyMs'),
                'lastResult': (latest or {}).get('message') or '',
                'failureCount': sum(
                    row.get('state') == 'failed'
                    for row in self.check_job.history(proxy['asset_id'])),
                'historyCount': len(self.check_job.history(proxy['asset_id'])),
                'conflict': conflict,
                'remark': proxy.get('remark') or '',
            })

        explicitly_bound_serials = {
            row['envSerial'] for row in public_stores
            if row.get('mappingState') == 'bound' and row.get('envSerial')
        }
        store_stats = {
            'total': len(public_stores),
            'hubBound': sum(row['mappingState'] == 'bound' for row in public_stores),
            'proxyNormal': sum(row['proxyStatus'] in ('正常', '使用中')
                               for row in public_stores),
            'proxyAbnormal': sum(row['proxyStatus'] in ('异常', '有问题')
                                 for row in public_stores),
            'conflicts': sum(bool(row['conflicts']) for row in public_stores),
            'hubOrphans': sum(
                str(env.get('serialNumber') or '') not in explicitly_bound_serials
                for env in hub_envs),
        }
        proxy_stats = {
            'total': len(public_proxies),
            'unused': sum(row['occupancyKnown'] and not row['occupiedShop']
                          for row in public_proxies),
            'inUse': sum(row['occupancyKnown'] and bool(row['occupiedShop'])
                         for row in public_proxies),
            'retest': sum(row['checkStatus'] == '待复测' for row in public_proxies),
            'abnormal': sum(row['checkStatus'] == '异常'
                            or row['assetStatus'] in ('异常', '有问题')
                            for row in public_proxies),
            'conflicts': sum(row['conflict'] for row in public_proxies),
        }
        proxy_type_catalog = []
        for definition in PROXY_TYPE_DEFINITIONS:
            code = definition['code']
            count = sum(
                row['proxyTypeCode'] == code for row in public_proxies)
            if definition['inventory_mode'] == 'dynamic':
                asset_count = None
                inventory_summary = '动态提取，不计固定库存'
            elif code == 'static_datacenter':
                provider_ready = any(
                    (sources.get(source_name) or {}).get('ready')
                    for source_name in ('proxyLedger', 'proxyProvider')
                )
                asset_count = count if provider_ready else None
                inventory_summary = (
                    '%s 条固定资产' % count if provider_ready
                    else '固定资产来源未就绪')
            elif count:
                asset_count = count
                inventory_summary = '%s 条固定资产' % count
            else:
                asset_count = None
                inventory_summary = '尚未接入资产台账'
            proxy_type_catalog.append({
                'typeCode': code,
                'typeLabel': definition['label'],
                'provider': definition['provider'],
                'usageScenarioCode': definition['usage_scenario_code'],
                'usageScenario': definition['usage_scenario'],
                'acquisitionModeCode': definition['acquisition_mode_code'],
                'acquisitionMode': definition['acquisition_mode'],
                'accessRequirement': definition['access_requirement'],
                'usageStatus': definition['usage_status'],
                'inventoryMode': definition['inventory_mode'],
                'assetCount': asset_count,
                'inventorySummary': inventory_summary,
            })
        return {
            'generatedAt': datetime.now(timezone.utc).isoformat(timespec='seconds'),
            'sources': sources,
            'stores': public_stores,
            'proxies': public_proxies,
            'storeStats': store_stats,
            'proxyStats': proxy_stats,
            'proxyTypeCatalog': proxy_type_catalog,
        }

    def stores_snapshot(self, force=False):
        snapshot = self._snapshot(force=force)
        return {
            'generatedAt': snapshot['generatedAt'],
            'sources': snapshot['sources'],
            'stats': snapshot['storeStats'],
            'rows': [dict(row) for row in snapshot['stores']],
            'phase': 'P0/P1 只读对账',
            'writesEnabled': False,
        }

    def proxies_snapshot(self, force=False):
        snapshot = self._snapshot(force=force)
        rows = []
        for source_row in snapshot['proxies']:
            row = dict(source_row)
            history = self.check_job.history(row['assetId'])
            latest = history[0] if history else None
            if latest:
                row.update({
                    'checkStatus': latest.get('checkStatus') or '待检测',
                    'lastCheckDevice': '当前电脑',
                    'lastCheckAt': latest.get('checkedAt') or '',
                    'latencyMs': latest.get('latencyMs'),
                    'lastResult': latest.get('message') or '',
                    'failureCount': sum(
                        item.get('state') == 'failed' for item in history),
                    'historyCount': len(history),
                })
            rows.append(row)
        stats = dict(snapshot['proxyStats'])
        stats['retest'] = sum(row['checkStatus'] == '待复测' for row in rows)
        stats['abnormal'] = sum(
            row['checkStatus'] == '异常'
            or row['assetStatus'] in ('异常', '有问题') for row in rows)
        return {
            'generatedAt': snapshot['generatedAt'],
            'sources': snapshot['sources'],
            'stats': stats,
            'rows': rows,
            'typeCatalog': [dict(item) for item in
                            snapshot['proxyTypeCatalog']],
            'checkDefaults': {
                'concurrency': 10, 'timeoutSec': 8,
                'retryCount': 1, 'maxItems': MAX_PROXY_CHECK_ITEMS,
            },
            'phase': 'P0/P1 本机只读检测',
            'writesEnabled': False,
        }

    def _load_selected_endpoints(self, asset_ids):
        snapshot = self._snapshot(force=False)
        known_assets = {row['assetId'] for row in snapshot['proxies']}
        protocol_by_asset = {
            row['assetId']: row.get('protocol') or 'SOCKS5'
            for row in snapshot['proxies']
            if row.get('healthCheckSupported')
        }
        requested = set(asset_ids)
        unknown = requested - known_assets
        if unknown:
            raise ValueError('所选代理 IP 已变化，请刷新列表后重试')
        unsupported = requested - set(protocol_by_asset)
        if unsupported:
            raise ValueError(
                '所选代理类型不支持固定资产本机检测；711 动态住宅代理在采购环境创建时由 API 提取')
        try:
            return self.reader.load_proxy_endpoints(
                asset_ids, protocol_by_asset=protocol_by_asset)
        except Exception as exc:
            if getattr(exc, 'code', None) == 99991672:
                raise ValueError(
                    '飞书普通表格尚未授权给“小犀代采”应用；请先开通 '
                    'sheets:spreadsheet:readonly 并授予 Webshare IP 总表访问权') from exc
            raise

    def start_proxy_checks(self, asset_ids, concurrency=10, timeout=8):
        return self.check_job.start(asset_ids, concurrency, timeout)

    def stop_proxy_checks(self):
        self.check_job.stop()

    def proxy_check_snapshot(self):
        return self.check_job.snapshot()

    def proxy_history(self, asset_id):
        return {'assetId': asset_id, 'rows': self.check_job.history(asset_id)}

    @staticmethod
    def _csv_bytes(headers, rows):
        output = StringIO()
        writer = csv.DictWriter(output, fieldnames=headers, extrasaction='ignore')
        writer.writeheader()
        writer.writerows(rows)
        return output.getvalue().encode('utf-8-sig')

    def store_export(self):
        rows = self.stores_snapshot()['rows']
        headers = [
            'shopName', 'platform', 'site', 'department', 'owner',
            'shopStatus', 'mappingState', 'hubGroup', 'envName', 'envSerial',
            'containerCodeMasked', 'proxyAddressMasked', 'proxySource',
            'proxyStatus', 'conflicts',
        ]
        exported = []
        for row in rows:
            safe = dict(row)
            safe['conflicts'] = '；'.join(row.get('conflicts') or [])
            exported.append(safe)
        return self._csv_bytes(headers, exported)

    def proxy_export(self):
        rows = self.proxies_snapshot()['rows']
        headers = [
            'assetId', 'addressMasked', 'proxyTypeCode', 'proxyType',
            'provider', 'usageScenarioCode', 'usageScenario',
            'acquisitionModeCode', 'acquisitionMode', 'accessRequirement',
            'country', 'protocol', 'assetStatus', 'checkStatus',
            'occupiedShop', 'department', 'browser', 'envSerial',
            'lastCheckDevice', 'lastCheckAt', 'latencyMs', 'lastResult',
            'failureCount', 'conflict', 'remark',
        ]
        return self._csv_bytes(headers, rows)
