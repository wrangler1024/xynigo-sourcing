# -*- coding: utf-8 -*-
"""采购工具服务入口：标准库 HTTP 服务 + 自动打开浏览器。

启动：python -m purchase_tool   （或打包后的 exe / 启动脚本）
API：
  GET  /                    操作页面
  GET  /api/hub-status      探测 HubStudio 是否在线
  GET  /api/groups          分组列表
  GET  /api/group-envs      指定分组的环境序号（查全部分组用）
  POST /api/query           {serials:[...],site:"MX|US"} 或 {group:"分组名",site}
  GET  /api/progress        查询进度与结果行
  POST /api/stop            停止当前批次
  POST /api/requery         {serial} 单行重查
  POST /api/register/validate 脱敏校验注册凭证文件
  POST /api/register/start  启动低并发注册
  GET  /api/register/progress 注册脱敏进度
  GET  /api/buyer-library 读取飞书买家号库脱敏元数据
  POST /api/buyer-library/import/parse 号商 xlsx 入库预检
  POST /api/buyer-library/import/commit 二次确认后写入买家号库
  POST /api/envbatch/parse  模块三 xlsx 严格解析（只返回脱敏计划）
  POST /api/envbatch/preview/start/retry-row 模块三预览/执行/单步重试
  GET  /api/envbatch/progress/export-mapping/export-tsv 模块三进度/导出
  GET  /api/export          ?format=xlsx|csv 下载结果
  GET/POST /api/config      本机配置（HubStudio 端口等，存 config.json）
"""
import base64
import binascii
import csv
from io import BytesIO, StringIO
import json
import os
import re
import secrets
import sys
import tempfile
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from . import __version__
from .buyer_library import BuyerLibraryJob, BuyerLibraryService
from .buyer_ledger_sync import validate_unified_schema
from .buyer_register import BuyerRegistrationTask, RegistrationOrchestrator
from .cloud_auth import LocalAuthError, LocalAuthService
from .excel_export import EXPORT_HEAD, export_bytes
from .env_batch import (BACKUP_MAX_COUNT, BACKUP_REMARK, BUYER_CODES,
                        BUYER_ROSTER, BatchEnvOrchestrator,
                        BackupEnvOrchestrator, DEFAULT_PROXY_LINK,
                        DEFAULT_SPLIT_BUYERS,
                        ResumeStateStore, backup_env_names,
                        backup_result_tsv_bytes,
                        batch_fingerprint, build_batch_plan,
                        envbatch_preflight,
                        mapping_workbook_bytes, normalize_backup_type,
                        normalize_buyer, parse_assignment,
                        normalize_env_site,
                        parse_vendor_workbook, require_envbatch_ready,
                        validate_accounts_site,
                        validate_assignment_template,
                        validate_backup_count,
                        validate_proxy_link, validate_purchase_tag)
from .extension_bridge import ExtensionBridge, ExtensionBridgeError
from .hub_api import HubStudioApi, DEFAULT_PORT
from .lark_credentials import (LarkCredentialError, LarkCredentials,
                               public_credential_status,
                               system_credential_store)
from .lark_links import (LarkLedgerTargetConfig, build_lark_base_link,
                         parse_lark_base_link, resolve_lark_ledger_link)
from .lark_ledger import LarkLedgerSink
from .lark_openapi import LarkOpenApiClient
from .lark_runtime import build_buyer_ledger_service
from .redaction import scrub_text
from .resource_center import ResourceCenterService
from .shein_query import QueryOrchestrator, normalize_site
from .updater import UpdateCoordinator

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
INDEX_HTML = os.path.join(BASE_DIR, 'web', 'index.html')
LOGO_PNG = os.path.join(BASE_DIR, 'web', 'xynigo-logo.png')
MASCOT_X_PNG = os.path.join(BASE_DIR, 'web', 'xynigo-mascot-x.png')
X_ICON_PNG = os.path.join(BASE_DIR, 'web', 'xynigo-x.png')
X_ICON_ICO = os.path.join(BASE_DIR, 'web', 'xynigo-x.ico')
EXTENSION_CONNECT_HTML = os.path.join(
    BASE_DIR, 'web', 'extension-connect.html')
EXTENSION_CONNECT_JS = os.path.join(
    BASE_DIR, 'web', 'extension-connect.js')
ENV_TEMPLATE_XLSX = os.path.join(
    BASE_DIR, 'web', '采购工具买家号入库模板.xlsx')
LARK_LEDGER_TEMPLATE_XLSX = os.path.join(
    BASE_DIR, 'web', '买家号统一台账模板.xlsx')
CONFIG_PATH = os.path.join(os.getcwd(), 'config.json')
LOG_DIR = os.path.join(os.getcwd(), '查询日志')

CONFIG_FIELDS = frozenset({
    'hubPort', 'serverPort', 'concurrency', 'importBuyerPlan',
    'verifySampleCount', 'hiddenQueryColumns', 'purchaseSite',
    'purchaseTag', 'purchaseTags', 'proxyLink', 'envCreateWorkers',
    'larkBuyerBaseToken', 'larkBuyerTableId',
    'larkBuyerTargetHost',
    'larkBuyerBaseName', 'larkBuyerTableName',
    'larkBuyerTargetVerified',
})
CONFIG_REQUEST_FIELDS = (CONFIG_FIELDS - {
    'larkBuyerBaseToken', 'larkBuyerTableId',
    'larkBuyerTargetHost',
    'larkBuyerBaseName', 'larkBuyerTableName',
    'larkBuyerTargetVerified'}) | {'proxyClear'}

PUBLIC_AUTH_API_PATHS = frozenset({
    '/api/auth/status',
    '/api/auth/start',
    '/api/auth/poll',
    '/api/auth/logout',
})
AUTH_PERMISSION_BY_PATH = {
    '/api/progress': 'fulfillment.order.read',
    '/api/query': 'fulfillment.order.read',
    '/api/stop': 'fulfillment.order.read',
    '/api/requery': 'fulfillment.order.read',
    '/api/requery-failed': 'fulfillment.order.read',
    '/api/screenshot': 'fulfillment.order.read',
    '/api/export': 'fulfillment.order.export',
    '/api/buyer-library': 'resource.buyer.read',
    '/api/buyer-library/import/parse': 'resource.buyer.import',
    '/api/buyer-library/import/commit': 'resource.buyer.import',
    '/api/register/progress': 'resource.buyer.import',
    '/api/register/validate': 'resource.buyer.import',
    '/api/register/start': 'resource.buyer.import',
    '/api/resources/stores': 'resource.store.read',
    '/api/resources/stores/export': 'resource.store.read',
    '/api/resources/proxies': 'resource.ip.read',
    '/api/resources/proxies/export': 'resource.ip.read',
    '/api/resources/proxies/check/history': 'resource.ip.read',
    '/api/resources/proxies/check/progress': 'resource.ip.test',
    '/api/resources/proxies/check/start': 'resource.ip.test',
    '/api/resources/proxies/check/stop': 'resource.ip.test',
    '/api/lark/config': 'system.lark_connection.manage',
    '/api/lark/open-target': 'system.lark_connection.manage',
    '/api/lark/target-metadata': 'system.lark_connection.manage',
    '/api/lark/preflight': 'system.lark_connection.manage',
    '/api/extension/pair/approve': 'operations.access',
    '/api/procurement/claims': 'procurement.execution.manage',
}
SUPER_ADMIN_ONLY_PERMISSIONS = frozenset({
    'system.lark_connection.manage',
    'system.integration.manage',
    'resource.ip.credential.manage',
})
AUTH_PERMISSION_BY_PREFIX = (
    ('/api/procurement/', 'procurement.request.read'),
    ('/api/admin/roles', 'system.role.manage'),
    ('/api/admin/permissions', 'system.role.manage'),
    ('/api/admin/sessions', 'system.member.manage'),
    ('/api/admin/members', 'system.member.manage'),
    ('/api/envbatch/', 'resource.environment.create'),
)


def admin_cloud_write_target(path):
    """Map same-origin browser POST routes to the cloud admin method/path."""
    cloud_path = '/v1/admin/' + path[len('/api/admin/'):]
    if path.startswith('/api/admin/roles/') and path.endswith('/rename'):
        return cloud_path[:-len('/rename')], 'PUT'
    if path.startswith('/api/admin/roles/') and path.endswith('/delete'):
        return cloud_path[:-len('/delete')], 'DELETE'
    if path == '/api/admin/roles':
        return cloud_path, 'POST'
    if path.endswith('/roles') or path.endswith('/permissions'):
        return cloud_path, 'PUT'
    return cloud_path, 'POST'


def default_config():
    legacy_tag = os.environ.get('XYNIGO_PURCHASE_TAG', '')
    mx_tag = os.environ.get('XYNIGO_PURCHASE_TAG_MX', legacy_tag)
    us_tag = os.environ.get('XYNIGO_PURCHASE_TAG_US', '')
    return {
        'hubPort': DEFAULT_PORT,
        'serverPort': 8765,
        'concurrency': 2,
        'importBuyerPlan': '1:新刚',
        'verifySampleCount': 3,
        'hiddenQueryColumns': ['envName', 'ip'],
        # Environment variables are migration/first-run defaults only. Once
        # saved, the local config file is the runtime source of truth.
        'purchaseSite': 'MX',
        'purchaseTag': mx_tag,
        'purchaseTags': {'MX': mx_tag, 'US': us_tag},
        'proxyLink': os.environ.get('XYNIGO_PROXY_LINK', ''),
        'envCreateWorkers': 5,
        # Base/table identifiers are local routing configuration.  The App
        # Secret lives in Keychain/DPAPI and is never written to config.json.
        'larkBuyerBaseToken': os.environ.get('XYNIGO_LARK_BASE_TOKEN', ''),
        'larkBuyerTableId': (os.environ.get('XYNIGO_LARK_TABLE_ID') or
                             os.environ.get('XYNIGO_LARK_TABLE_ID_MX', '')),
        'larkBuyerTargetHost': '',
        'larkBuyerBaseName': '',
        'larkBuyerTableName': '',
        'larkBuyerTargetVerified': False,
    }


def purchase_tags_from_config(cfg):
    """Return the MX/US group map, migrating the legacy MX-only field."""
    cfg = cfg if isinstance(cfg, dict) else {}
    result = {'MX': '', 'US': ''}
    raw = cfg.get('purchaseTags')
    if isinstance(raw, dict):
        for site in result:
            result[site] = str(raw.get(site) or '').strip()
    legacy = str(cfg.get('purchaseTag') or '').strip()
    if legacy and not result['MX']:
        result['MX'] = legacy
    return result


def purchase_tag_for_site(cfg, site):
    site = normalize_env_site(site)
    return purchase_tags_from_config(cfg).get(site, '')


def effective_proxy_link(cfg):
    """代理提取链接：自定义优先，未配置/已清除回落到内置默认（前期写死决策）。"""
    return str((cfg or {}).get('proxyLink') or '').strip() or DEFAULT_PROXY_LINK


def load_config():
    cfg = default_config()
    try:
        with open(CONFIG_PATH, encoding='utf-8') as f:
            saved = json.load(f)
        if isinstance(saved, dict):
            cfg.update({key: value for key, value in saved.items()
                        if key in CONFIG_FIELDS})
    except Exception:
        pass
    return cfg


def save_config(cfg):
    unknown = set(cfg) - CONFIG_FIELDS
    if unknown:
        raise ValueError('配置包含不允许保存的字段')
    path = os.path.abspath(CONFIG_PATH)
    parent = os.path.dirname(path)
    os.makedirs(parent, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix='.config-', suffix='.tmp', dir=parent)
    try:
        try:
            os.fchmod(fd, 0o600)
        except (AttributeError, OSError):
            pass
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            json.dump(cfg, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def public_config(cfg):
    result = {key: value for key, value in cfg.items()
              if key in CONFIG_FIELDS and key not in {
                  'proxyLink', 'larkBuyerBaseToken', 'larkBuyerTableId',
                  'larkBuyerTargetHost',
                  'larkBuyerBaseName', 'larkBuyerTableName',
                  'larkBuyerTargetVerified'}}
    result['proxyConfigured'] = bool(effective_proxy_link(cfg))
    result['proxySource'] = ('custom' if str(
        (cfg or {}).get('proxyLink') or '').strip() else 'default')
    try:
        site = normalize_env_site(cfg.get('purchaseSite') or 'MX')
    except ValueError:
        site = 'MX'
    tags = purchase_tags_from_config(cfg)
    result['purchaseSite'] = site
    result['purchaseTags'] = tags
    result['purchaseTag'] = tags[site]
    result['buyers'] = [{'name': name, 'code': code}
                        for name, code in BUYER_ROSTER]
    result['buyerDefaultSplit'] = list(DEFAULT_SPLIT_BUYERS)
    result['backupMaxCount'] = BACKUP_MAX_COUNT
    result['larkLedgerTargetConfigured'] = bool(
        str((cfg or {}).get('larkBuyerBaseToken') or '').strip()
        and str((cfg or {}).get('larkBuyerTableId') or '').strip())
    return result


def updated_config(old_cfg, body):
    if not isinstance(body, dict):
        raise ValueError('配置请求必须是 JSON 对象')
    unknown = set(body) - CONFIG_REQUEST_FIELDS
    if unknown:
        raise ValueError('配置包含不允许保存的字段')
    cfg = dict(default_config())
    cfg.update({key: value for key, value in old_cfg.items()
                if key in CONFIG_FIELDS})
    for key in CONFIG_FIELDS - {
            'proxyLink', 'purchaseSite', 'purchaseTag', 'purchaseTags',
            'larkBuyerBaseToken', 'larkBuyerTableId',
            'larkBuyerTargetHost',
            'larkBuyerBaseName', 'larkBuyerTableName',
            'larkBuyerTargetVerified'}:
        if key in body:
            cfg[key] = body[key]

    try:
        cfg['hubPort'] = int(cfg.get('hubPort', DEFAULT_PORT))
        cfg['serverPort'] = int(cfg.get('serverPort', 8765))
    except (TypeError, ValueError) as exc:
        raise ValueError('端口必须是 1-65535 的整数') from exc
    if not 1 <= cfg['hubPort'] <= 65535 or not 1 <= cfg['serverPort'] <= 65535:
        raise ValueError('端口必须是 1-65535 的整数')
    try:
        cfg['concurrency'] = max(1, min(5, int(cfg.get('concurrency', 2))))
    except (TypeError, ValueError):
        cfg['concurrency'] = 2
    try:
        cfg['verifySampleCount'] = max(
            0, min(10, int(cfg.get('verifySampleCount', 3))))
    except (TypeError, ValueError):
        cfg['verifySampleCount'] = 3
    if 'envCreateWorkers' in body:
        try:
            workers = int(body.get('envCreateWorkers'))
        except (TypeError, ValueError) as exc:
            raise ValueError('模块三建环境并发数必须是 1-10 的整数') from exc
        if not 1 <= workers <= 10:
            raise ValueError('模块三建环境并发数必须是 1-10 的整数')
        cfg['envCreateWorkers'] = workers
    if 'importBuyerPlan' in body:
        cfg['importBuyerPlan'] = validate_assignment_template(
            body.get('importBuyerPlan'))
    else:
        cfg['importBuyerPlan'] = str(
            cfg.get('importBuyerPlan') or '1:新刚')[:200]
    hidden = cfg.get('hiddenQueryColumns') or []
    if not isinstance(hidden, list):
        hidden = []
    cfg['hiddenQueryColumns'] = [
        name for name in ('envName', 'ip') if name in hidden]
    purchase_site = normalize_env_site(
        body.get('purchaseSite', old_cfg.get('purchaseSite') or 'MX'))
    purchase_tags = purchase_tags_from_config(old_cfg)
    submitted_tags = body.get('purchaseTags')
    if submitted_tags is not None:
        if not isinstance(submitted_tags, dict):
            raise ValueError('purchaseTags 必须是 MX/US 分组对象')
        unknown_sites = set(submitted_tags) - {'MX', 'US'}
        if unknown_sites:
            raise ValueError('采购分组包含不支持的站点')
        for site, value in submitted_tags.items():
            purchase_tags[site] = str(value or '').strip()
    if 'purchaseTag' in body:
        purchase_tags[purchase_site] = str(body.get('purchaseTag') or '').strip()
    for site, tag in tuple(purchase_tags.items()):
        purchase_tags[site] = validate_purchase_tag(tag) if tag else ''
    cfg['purchaseSite'] = purchase_site
    cfg['purchaseTags'] = purchase_tags
    # Keep the legacy field synchronized for older packages/config readers.
    cfg['purchaseTag'] = purchase_tags[purchase_site]

    if 'proxyClear' in body and not isinstance(body['proxyClear'], bool):
        raise ValueError('显式清除代理配置必须是布尔值')
    if body.get('proxyClear') is True:
        cfg['proxyLink'] = ''
    else:
        submitted_proxy = str(body.get('proxyLink') or '')
        if submitted_proxy.strip():
            cfg['proxyLink'] = validate_proxy_link(submitted_proxy)
        else:
            cfg['proxyLink'] = str(old_cfg.get('proxyLink') or '').strip()
    return cfg


def _validate_lark_target_value(value, label, table=False):
    value = str(value or '').strip()
    if not value or not re.fullmatch(r'[A-Za-z0-9_-]{8,160}', value):
        raise ValueError('%s格式无效' % label)
    if table and not value.startswith('tbl'):
        raise ValueError('飞书数据表 ID 必须以 tbl 开头')
    return value


def updated_lark_config(old_cfg, body, resolved_target=None):
    if not isinstance(body, dict):
        raise ValueError('飞书配置请求必须是 JSON 对象')
    allowed = {
        'appId', 'appSecret', 'ledgerUrl',
        'clearCredential', 'clearLedgerTarget',
    }
    if set(body) - allowed:
        raise ValueError('飞书配置包含不允许保存的字段')
    for flag in ('clearCredential', 'clearLedgerTarget'):
        if flag in body and not isinstance(body[flag], bool):
            raise ValueError('飞书清除配置必须是布尔值')
    cfg = dict(old_cfg)
    submitted_url = str(body.get('ledgerUrl') or '').strip()
    if body.get('clearLedgerTarget') and submitted_url:
        raise ValueError('清除台账目标与填写新链接不能同时选择')
    if body.get('clearLedgerTarget'):
        cfg['larkBuyerBaseToken'] = ''
        cfg['larkBuyerTableId'] = ''
        cfg['larkBuyerTargetHost'] = ''
        cfg['larkBuyerBaseName'] = ''
        cfg['larkBuyerTableName'] = ''
        cfg['larkBuyerTargetVerified'] = False
    elif submitted_url:
        if not isinstance(resolved_target, LarkLedgerTargetConfig):
            raise ValueError('飞书多维表格链接尚未完成解析')
        cfg['larkBuyerBaseToken'] = _validate_lark_target_value(
            resolved_target.base_token, '飞书 Base Token')
        cfg['larkBuyerTableId'] = _validate_lark_target_value(
            resolved_target.table_id, '飞书数据表 ID', table=True)
        cfg['larkBuyerTargetHost'] = resolved_target.source_hostname
        cfg['larkBuyerBaseName'] = ''
        cfg['larkBuyerTableName'] = ''
        cfg['larkBuyerTargetVerified'] = False
    if (body.get('clearCredential')
            or str(body.get('appId') or '').strip()):
        cfg['larkBuyerTargetVerified'] = False
    return cfg


def submitted_lark_credentials(body):
    app_id = str((body or {}).get('appId') or '').strip()
    app_secret = str((body or {}).get('appSecret') or '').strip()
    if bool(app_id) != bool(app_secret):
        raise ValueError('App ID 与 App Secret 必须同时填写')
    if (body or {}).get('clearCredential') and app_id:
        raise ValueError('清除应用凭证与填写新凭证不能同时选择')
    if not app_id:
        return None
    try:
        return LarkCredentials(app_id, app_secret)
    except LarkCredentialError as exc:
        raise ValueError(str(exc)) from exc


def resolve_submitted_lark_target(body, credential_store,
                                  client_factory=LarkOpenApiClient):
    url = str((body or {}).get('ledgerUrl') or '').strip()
    if not url:
        return None
    if (body or {}).get('clearCredential'):
        raise ValueError('清除应用凭证时不能同时解析新台账链接')
    reference = parse_lark_base_link(url)
    if reference.kind == 'base':
        return resolve_lark_ledger_link(url)
    credentials = submitted_lark_credentials(body) or credential_store.load()
    if credentials is None:
        raise ValueError('Wiki 链接需要先配置 App ID 与 App Secret')
    client = client_factory(
        credential_provider=lambda: credentials,
        base_token='', table_id='')
    return resolve_lark_ledger_link(url, client)


def public_lark_config(cfg, credential_store):
    result = public_credential_status(credential_store)
    result['ledgerTargetConfigured'] = bool(
        str((cfg or {}).get('larkBuyerBaseToken') or '').strip()
        and str((cfg or {}).get('larkBuyerTableId') or '').strip())
    result['ready'] = bool(
        result['credentialConfigured'] and result['ledgerTargetConfigured'])
    # Identifiers stay private; blank inputs in the UI mean "preserve".
    result['baseTokenConfigured'] = bool(
        str((cfg or {}).get('larkBuyerBaseToken') or '').strip())
    result['tableIdConfigured'] = bool(
        str((cfg or {}).get('larkBuyerTableId') or '').strip())
    result['targetBaseName'] = str(
        (cfg or {}).get('larkBuyerBaseName') or '').strip()
    result['targetTableName'] = str(
        (cfg or {}).get('larkBuyerTableName') or '').strip()
    result['targetVerified'] = bool(
        (cfg or {}).get('larkBuyerTargetVerified')
        and result['targetBaseName'] and result['targetTableName'])
    return result


def public_lark_runtime_status(cfg, credential_store):
    """Expose only the connection readiness needed by business modules."""
    configured = public_lark_config(cfg, credential_store)
    return {
        'ready': configured['ready'],
        'ledgerTargetConfigured': configured['ledgerTargetConfigured'],
        'targetBaseName': configured['targetBaseName'],
        'targetTableName': configured['targetTableName'],
        'targetVerified': configured['targetVerified'],
    }


def lark_target_link(cfg):
    """Return the validated browser URL for the locally configured target."""
    return build_lark_base_link(
        (cfg or {}).get('larkBuyerBaseToken'),
        (cfg or {}).get('larkBuyerTableId'),
        (cfg or {}).get('larkBuyerTargetHost'))


def _clean_lark_target_name(value, label):
    value = ''.join(
        char for char in str(value or '').strip()
        if char >= ' ' and char != '\x7f')[:160]
    if not value:
        raise ValueError('%s为空' % label)
    return value


def refreshed_lark_target_labels(cfg, credential_store, client=None):
    """Verify the configured target and persist display-only names."""
    if client is None:
        client = build_buyer_ledger_service(
            cfg, credential_store).client
    metadata = client.get_target_metadata()
    refreshed = dict(cfg)
    refreshed['larkBuyerBaseName'] = _clean_lark_target_name(
        metadata.get('base_name'), '飞书多维表格名称')
    refreshed['larkBuyerTableName'] = _clean_lark_target_name(
        metadata.get('table_name'), '飞书数据表名称')
    refreshed['larkBuyerTargetVerified'] = True
    return refreshed


def public_error(exc):
    """Last-resort API error scrubber; domain services should redact first."""
    return scrub_text(exc)[:300]


class HubStatusCache(object):
    """Cache connection state so independent UI polls cannot flood HubStudio."""

    def __init__(self, hub_getter, ttl_seconds=12.0):
        self.hub_getter = hub_getter
        self.ttl_seconds = float(ttl_seconds)
        self.lock = threading.Lock()
        self.value = None
        self.error = ''
        self.checked_at = 0.0

    def reset(self):
        with self.lock:
            self.value = None
            self.error = ''
            self.checked_at = 0.0

    def check(self, force=False):
        with self.lock:
            now = time.monotonic()
            if (not force and self.value is not None
                    and now - self.checked_at < self.ttl_seconds):
                return self.value, self.error
            ok, err = self.hub_getter().ping_detail()
            # E010205 is a rate-limit response, not proof that the local
            # client disconnected. Preserve the last observed state.
            if (not ok and 'E010205' in (err or '')
                    and self.value is not None):
                self.checked_at = now
                return self.value, self.error
            self.value = bool(ok)
            self.error = err or ''
            self.checked_at = now
            return self.value, self.error


class AppState(object):
    """进程级共享状态：配置 + HubStudio 连接 + 编排器。"""

    def __init__(self, credential_store=None, auth_service=None,
                 extension_bridge=None):
        cfg = load_config()
        self.cfg = cfg
        self.auth = auth_service or LocalAuthService()
        self.extension_bridge = extension_bridge or ExtensionBridge()
        self.lark_credentials = credential_store or system_credential_store()
        self.hub = HubStudioApi(port=cfg['hubPort'])
        self._hub_status = HubStatusCache(lambda: self.hub)
        self.orch = QueryOrchestrator(
            self.hub, log_dir=LOG_DIR,
            concurrency=cfg.get('concurrency', 2))
        self.reg_job = RegistrationJob(
            lambda: self.hub,
            ledger_sink_factory=lambda: LarkLedgerSink(
                build_buyer_ledger_service(
                    self.cfg, self.lark_credentials).client))
        self.buyer_library = BuyerLibraryJob(
            lambda: BuyerLibraryService(
                build_buyer_ledger_service(
                    self.cfg, self.lark_credentials).client))
        self.env_job = EnvBatchJob(
            lambda: self.hub, lambda: self.cfg,
            ledger_sync_factory=lambda: build_buyer_ledger_service(
                self.cfg, self.lark_credentials))
        self.backup_job = BackupEnvJob(lambda: self.hub, lambda: self.cfg)
        self.resources = ResourceCenterService(
            lambda: self.hub, self.lark_credentials.load)
        self.updates = UpdateCoordinator(
            os.environ.get('XYNIGO_INSTALL_DIR'), __version__)

    def reconnect_hub(self):
        self.orch.close()
        self.hub = HubStudioApi(port=self.cfg['hubPort'])
        self.orch = QueryOrchestrator(
            self.hub, log_dir=LOG_DIR,
            concurrency=self.cfg.get('concurrency', 2))
        self._hub_status.reset()
        self.resources.invalidate()
        return self.hub_status(force=True)[0]

    def hub_status(self, force=False):
        return self._hub_status.check(force=force)


def build_state():
    return AppState()


STATE = None


class RegistrationJob(object):
    """注册模块后台任务：只保留脱敏进度，不保存原始凭证。"""

    def __init__(self, hub_getter, ledger_sink_factory=None):
        self.hub_getter = hub_getter
        self.ledger_sink_factory = ledger_sink_factory
        self.lock = threading.Lock()
        self.running = False
        self.started_at = None
        self.finished_at = None
        self.rows = []

    @staticmethod
    def parse_tasks(raw_tasks):
        if not isinstance(raw_tasks, list) or not raw_tasks:
            raise ValueError('注册任务必须是非空数组')
        return [BuyerRegistrationTask.from_dict(x) for x in raw_tasks]

    def validate(self, raw_tasks):
        tasks = self.parse_tasks(raw_tasks)
        return [{'emailMasked': t.safe_name,
                 'env': t.env_serial or t.env_name,
                 'site': t.site} for t in tasks]

    def snapshot(self):
        with self.lock:
            end_at = time.time() if self.running else self.finished_at
            elapsed = int(max(0, end_at - self.started_at)) \
                if self.started_at and end_at else 0
            return {
                'running': self.running,
                'elapsedSec': elapsed,
                'rows': [dict(x) for x in self.rows],
            }

    def start(self, raw_tasks, accept_terms=False,
              acknowledge_ms_privacy=False, keep_open=False,
              write_lark_ledger=False, confirm_lark_write=False):
        if self.running:
            raise RuntimeError('已有注册任务在进行')
        if not accept_terms:
            raise ValueError('真实注册必须确认 SHEIN 条款')
        tasks = self.parse_tasks(raw_tasks)
        if write_lark_ledger and any(not t.record_id for t in tasks):
            raise ValueError(
                '勾选台账回写时，每个任务都必须提供 record_id')
        if write_lark_ledger and not confirm_lark_write:
            raise ValueError('飞书台账回写必须单独二次确认')
        if write_lark_ledger and self.ledger_sink_factory is None:
            raise ValueError('飞书 OpenAPI 回写尚未配置')
        ledger_sink = None
        if write_lark_ledger:
            ledger_sink = self.ledger_sink_factory()
            ledger_sink.preflight()
        with self.lock:
            self.running = True
            self.started_at = time.time()
            self.finished_at = None
            self.rows = [{
                'emailMasked': t.safe_name,
                'state': 'pending',
                'site': t.site,
                'envSerial': t.env_serial,
                'envName': t.env_name,
                'message': '', 'manualCode': ''} for t in tasks]

        def worker():
            runner = RegistrationOrchestrator(
                self.hub_getter(), accept_terms=True,
                acknowledge_ms_privacy=acknowledge_ms_privacy,
                ledger_sink=ledger_sink,
                close_on_success=not keep_open)
            try:
                for index, task in enumerate(tasks):
                    with self.lock:
                        self.rows[index]['state'] = 'running'
                    result = runner.run_one(task)
                    with self.lock:
                        self.rows[index] = {
                            'emailMasked': result.email_masked,
                            'state': result.state,
                            'site': result.site,
                            'envSerial': result.env_serial,
                            'envName': result.env_name,
                            'message': result.message,
                            'manualCode': result.manual_code,
                        }
                    if index + 1 < len(tasks):
                        time.sleep(10)
            finally:
                # tasks 只被当前线程持有，退出后即可回收。
                with self.lock:
                    self.finished_at = time.time()
                    self.running = False

        threading.Thread(target=worker, daemon=True).start()
        return len(tasks)


def _mask_order(order_no):
    value = str(order_no or '')
    if len(value) < 7:
        return '***'
    return value[:3] + '***' + value[-3:]


LEDGER_PASTE_COLUMNS = (
    # 飞书「买家号（统一）」默认「表格」视图的连续列契约。
    # 直贴文件从「站点」列开始，不包含左侧自动编号「账号ID」。
    '站点', '邮箱账号', '密码', '接码Key链接', 'Cookie', '号商购买单号',
    '购买日期', '账号状态', '绑定环境', '环境分组名', '环境序号',
    '采购员', '绑定时间')


def ledger_tsv_bytes(rows, site, purchase_date, environment_group):
    """生成对齐飞书统一台账的无表头直贴 TSV（含凭证）。"""
    site = normalize_env_site(site)
    environment_group = str(environment_group or '').strip()
    if not environment_group:
        raise ValueError('环境分组名不能为空')
    purchase_date = str(purchase_date or '').strip()
    try:
        parsed_purchase_date = time.strptime(purchase_date, '%Y%m%d')
    except (TypeError, ValueError) as exc:
        raise ValueError('购买日期必须是 YYYYMMDD') from exc
    purchase_date_text = time.strftime('%Y-%m-%d', parsed_purchase_date)
    output = StringIO(newline='')
    writer = csv.writer(output, dialect='excel-tab', lineterminator='\r\n')
    for row in rows:
        complete = row.state == 'done'
        values = {
            '站点': site,
            '邮箱账号': row.account.email,
            '密码': row.account.password,
            '接码Key链接': row.account.key_url,
            'Cookie': row.account.cookie_text,
            '号商购买单号': row.account.order_no,
            '购买日期': purchase_date_text,
            '账号状态': '已绑定' if complete else '未绑定',
            '绑定环境': row.env_name if complete else '',
            '环境分组名': environment_group if complete else '',
            '环境序号': row.serial_number if complete else '',
            '采购员': row.account.buyer,
            '绑定时间': row.binding_time if complete else '',
        }
        writer.writerow([values[name] for name in LEDGER_PASTE_COLUMNS])
    return ('\ufeff' + output.getvalue()).encode('utf-8')


def ledger_tsv_filename(site, purchase_date):
    site = normalize_env_site(site)
    return ('台账直贴_统一表_%s_%s_无表头_从站点列开始.tsv' %
            (site, purchase_date))


class EnvBatchJob(object):
    """模块三后台任务：凭证仅保存在短生命周期内存对象。"""

    MAX_UPLOAD_BYTES = 20 * 1024 * 1024
    PENDING_TTL_SECONDS = 30 * 60
    RESULT_CREDENTIAL_TTL_SECONDS = 15 * 60

    def __init__(self, hub_getter, config_getter=load_config,
                 ledger_sync_factory=None):
        self.hub_getter = hub_getter
        self.config_getter = config_getter
        self.ledger_sync_factory = ledger_sync_factory
        self.lock = threading.Lock()
        self.pending = {}
        self.running = False
        self.started_at = None
        self.finished_at = None
        self.rows = []
        self.runner = None
        self.summary = {}
        self.ip_checks = []
        self.fatal_error = ''
        self.mapping_data = None
        self.mapping_name = ''
        self.tsv_data = None
        self.tsv_name = ''
        self.ledger_enabled = False
        self.ledger_summary = {
            'enabled': False, 'running': False, 'total': 0,
            'created': 0, 'updated': 0, 'confirmed': 0,
            'conflict': 0, 'pending': 0, 'rows': [], 'error': '',
        }
        self._ledger_service = None
        self._sensitive_timer = None
        self._sensitive_generation = 0

    def _runtime_config(self, site='MX'):
        site = normalize_env_site(site)
        cfg = dict(self.config_getter() or {})
        try:
            workers = max(1, min(10, int(cfg.get('envCreateWorkers') or 5)))
        except (TypeError, ValueError):
            workers = 5
        return {
            'site': site,
            'purchaseTag': purchase_tag_for_site(cfg, site),
            'proxyLink': effective_proxy_link(cfg),
            'workers': workers,
        }

    def preflight(self, site='MX'):
        runtime = self._runtime_config(site)
        return envbatch_preflight(
            self.hub_getter(), runtime['purchaseTag'], runtime['proxyLink'],
            site=runtime['site'])

    def _clean_pending(self):
        cutoff = time.time() - self.PENDING_TTL_SECONDS
        expired = [token for token, item in self.pending.items()
                   if item['createdAt'] < cutoff]
        for token in expired:
            self._discard_pending(token)

    def _discard_pending(self, token):
        item = self.pending.pop(token, None)
        if not item:
            return
        item['source'] = b''
        for account in item.get('accounts') or []:
            account.password = ''
            account.key_url = ''
            account.cookie_text = ''

    def _expire_pending(self, token):
        with self.lock:
            self._discard_pending(token)

    @staticmethod
    def _decode_xlsx(content_base64):
        try:
            data = base64.b64decode(
                str(content_base64 or '').encode('ascii'), validate=True)
        except (ValueError, UnicodeEncodeError, binascii.Error) as exc:
            raise ValueError('xlsx 上传内容不是合法 Base64') from exc
        if not data:
            raise ValueError('xlsx 上传内容为空')
        if len(data) > EnvBatchJob.MAX_UPLOAD_BYTES:
            raise ValueError('xlsx 超过 20MB，拒绝载入')
        return data

    def parse(self, filename, content_base64):
        source = self._decode_xlsx(content_base64)
        accounts = parse_vendor_workbook(BytesIO(source))
        token = secrets.token_urlsafe(24)
        with self.lock:
            self._clean_pending()
            self.pending[token] = {
                'filename': os.path.basename(str(filename or '号商名单.xlsx')),
                'source': source,
                'accounts': accounts,
                'createdAt': time.time(),
            }
        timer = threading.Timer(
            self.PENDING_TTL_SECONDS, self._expire_pending, args=(token,))
        timer.daemon = True
        timer.start()
        return {
            'planId': token,
            'count': len(accounts),
            'cookieCount': sum(bool(item.cookie_text) for item in accounts),
            'passwordKindCount': len({item.password for item in accounts}),
            'duplicateCount': 0,
            'issueCount': 0,
            'orderCount': len(accounts),
            'preview': [{
                'emailMasked': item.safe_email,
                'orderMasked': _mask_order(item.order_no),
                'cookieBytes': len(item.cookie_text.encode('utf-8')),
            } for item in accounts[:5]],
        }

    def preview(self, plan_id, assignment, purchase_date, site='MX'):
        with self.lock:
            self._clean_pending()
            pending = self.pending.get(plan_id)
        if not pending:
            raise ValueError('解析计划已过期，请重新选择 xlsx')
        parse_assignment(assignment, len(pending['accounts']))
        runtime = self._runtime_config(site)
        hub = self.hub_getter()
        require_envbatch_ready(
            hub, runtime['purchaseTag'], runtime['proxyLink'],
            site=runtime['site'])
        existing = hub.env_list(runtime['purchaseTag'])
        all_existing = hub.env_list()
        plan = build_batch_plan(
            pending['accounts'], assignment, existing_envs=existing,
            site=runtime['site'], purchase_date=purchase_date,
            all_existing_envs=all_existing)
        return [{
            'emailMasked': row.account.safe_email,
            'buyer': row.account.buyer,
            'envName': row.env_name,
            'recoveredExisting': row.recovered_existing,
        } for row in plan]

    @staticmethod
    def _safe_error(exc, accounts, extra_secrets=()):
        text = str(exc)
        for account in accounts:
            for value in (account.email, account.password,
                          account.key_url, account.cookie_text):
                if value:
                    text = text.replace(value, '<redacted>')
        for value in extra_secrets:
            if value:
                text = text.replace(value, '<redacted>')
        from .redaction import scrub_text
        return scrub_text(text)[:300]

    def _set_rows(self, rows):
        with self.lock:
            self.rows = [dict(row) for row in rows]

    @staticmethod
    def _wipe_runner_credentials(runner):
        if runner:
            for row in runner.rows:
                row.account.password = ''
                row.account.key_url = ''
                row.account.cookie_text = ''

    def _cancel_sensitive_cleanup_locked(self):
        self._sensitive_generation += 1
        if self._sensitive_timer:
            self._sensitive_timer.cancel()
        self._sensitive_timer = None

    def _clear_sensitive(self, expected_generation=None, runner=None):
        with self.lock:
            if (expected_generation is not None
                    and expected_generation != self._sensitive_generation):
                return
            if expected_generation is None:
                self._cancel_sensitive_cleanup_locked()
            else:
                self._sensitive_timer = None
            self.tsv_data = None
            self._wipe_runner_credentials(runner or self.runner)
            self._ledger_service = None

    def _schedule_sensitive_cleanup(self, runner=None):
        with self.lock:
            self._cancel_sensitive_cleanup_locked()
            generation = self._sensitive_generation
            timer = threading.Timer(
                self.RESULT_CREDENTIAL_TTL_SECONDS,
                self._clear_sensitive,
                args=(generation, runner or self.runner))
            timer.daemon = True
            self._sensitive_timer = timer
        timer.start()

    @staticmethod
    def _ledger_failure_summary(rows, site, error):
        done_rows = [row for row in rows if row.state == 'done']
        return {
            'enabled': True,
            'running': False,
            'total': len(done_rows),
            'created': 0,
            'updated': 0,
            'confirmed': 0,
            'conflict': 0,
            'pending': len(done_rows),
            'error': error,
            'rows': [{
                'accountId': row.account.account_id,
                'rowNumber': row.account.row_number,
                'emailMasked': row.account.safe_email,
                'site': site,
                'state': 'pending',
                'message': error,
            } for row in done_rows],
        }

    def _sync_ledger_rows(self, service, rows, site, purchase_date,
                          environment_group):
        done_rows = [row for row in rows if row.state == 'done']
        with self.lock:
            self.ledger_summary.update({
                'enabled': True, 'running': True, 'total': len(done_rows),
                'error': '',
            })
        try:
            result = service.sync(
                done_rows, site, purchase_date, environment_group)
            result.update({'enabled': True, 'running': False, 'error': ''})
        except Exception as exc:
            error = self._safe_error(
                exc, [row.account for row in done_rows])
            result = self._ledger_failure_summary(done_rows, site, error)
        with self.lock:
            self.ledger_summary = result
        return result

    def start(self, plan_id, assignment, purchase_date,
              verify_sample_count=3, confirm_write=False, site='MX',
              write_lark_ledger=False, confirm_lark_write=False):
        if not confirm_write:
            raise ValueError('正式执行必须二次确认 HubStudio 写入')
        if write_lark_ledger and not confirm_lark_write:
            raise ValueError('飞书台账回写必须单独二次确认')
        if write_lark_ledger and self.ledger_sync_factory is None:
            raise ValueError('飞书 OpenAPI 回写尚未配置')
        try:
            verify_sample_count = max(0, int(verify_sample_count))
        except (TypeError, ValueError) as exc:
            raise ValueError('后台出口 IP 检测数量必须是非负整数') from exc
        with self.lock:
            if self.running:
                raise RuntimeError('已有模块三任务在进行')
            self._clean_pending()
            pending = self.pending.get(plan_id)
            if not pending:
                raise ValueError('解析计划已过期，请重新选择 xlsx')
            account_count = len(pending['accounts'])
            verify_sample_count = min(verify_sample_count, account_count)
        parse_assignment(assignment, account_count)
        runtime = self._runtime_config(site)
        # 正式执行同步校验：站点与账号 Cookie 域冲突在消费计划前整批拒收
        validate_accounts_site(pending['accounts'], runtime['site'])
        hub = self.hub_getter()
        # Must finish every read-only prerequisite before consuming planId or
        # launching a worker that can issue HubStudio writes.
        require_envbatch_ready(
            hub, runtime['purchaseTag'], runtime['proxyLink'],
            site=runtime['site'])
        # 全 HubStudio 严格查重必须在消费 planId 和启动写线程前完成。
        # 目标分组列表用于同组幂等恢复；无过滤列表用于发现其他分组。
        selected_existing = hub.env_list(runtime['purchaseTag'])
        all_existing = hub.env_list()
        checked_plan = build_batch_plan(
            pending['accounts'], assignment,
            existing_envs=selected_existing,
            site=runtime['site'], purchase_date=purchase_date,
            all_existing_envs=all_existing)
        ledger_service = None
        if write_lark_ledger:
            ledger_service = self.ledger_sync_factory()
            ledger_preflight = ledger_service.preflight_plan(
                checked_plan, runtime['site'], runtime['purchaseTag'])
            if ledger_preflight.get('conflicts'):
                raise ValueError(
                    '飞书统一台账发现 %d 条双键或站点冲突，已阻止建环境' %
                    ledger_preflight['conflicts'])
        with self.lock:
            if self.running:
                raise RuntimeError('已有模块三任务在进行')
            self._clean_pending()
            pending = self.pending.pop(plan_id, None)
            if not pending:
                raise ValueError('解析计划已过期，请重新选择 xlsx')
            self.running = True
            self.started_at = time.time()
            self.finished_at = None
            self.rows = []
            self.summary = {}
            self.ip_checks = []
            self.fatal_error = ''
            self.mapping_data = None
            self._cancel_sensitive_cleanup_locked()
            self._wipe_runner_credentials(self.runner)
            self.runner = None
            self.tsv_data = None
            self.ledger_enabled = bool(write_lark_ledger)
            self._ledger_service = ledger_service
            self.ledger_summary = {
                'enabled': bool(write_lark_ledger),
                'running': False,
                'total': 0,
                'created': 0,
                'updated': 0,
                'confirmed': 0,
                'conflict': 0,
                'pending': 0,
                'rows': [],
                'error': '',
            }
        source = pending['source']
        accounts = pending['accounts']
        batch_id = batch_fingerprint(
            source, assignment, runtime['site'], purchase_date)
        pending['source'] = b''

        def worker():
            runner = None
            try:
                runner = BatchEnvOrchestrator(
                    hub, purchase_tag=runtime['purchaseTag'],
                    proxy_link=runtime['proxyLink'], site=runtime['site'],
                    purchase_date=purchase_date,
                    state_store=ResumeStateStore(batch_id),
                    on_progress=self._set_rows,
                    max_workers=runtime['workers'])
                with self.lock:
                    self.runner = runner
                runner.prepare(accounts, assignment)
                result_rows = runner.run()
                mapping = mapping_workbook_bytes(result_rows)
                checks = runner.verify_ips(verify_sample_count)
                tsv = ledger_tsv_bytes(
                    result_rows, runtime['site'], purchase_date,
                    runtime['purchaseTag'])
                done = sum(row.state == 'done' for row in result_rows)
                if write_lark_ledger:
                    self._sync_ledger_rows(
                        ledger_service, result_rows,
                        runtime['site'], purchase_date,
                        runtime['purchaseTag'])
                with self.lock:
                    self.mapping_data = mapping
                    self.mapping_name = '绑定映射清单_%s.xlsx' % purchase_date
                    self.tsv_data = tsv
                    self.tsv_name = ledger_tsv_filename(
                        runtime['site'], purchase_date)
                    self.ip_checks = checks
                    self.summary = {
                        'total': len(result_rows),
                        'done': done,
                        'failed': len(result_rows) - done,
                        'ipOk': sum(bool(item.get('ok')) for item in checks),
                        'ipTotal': len(checks),
                    }
                self._schedule_sensitive_cleanup(runner)
            except Exception as exc:
                with self.lock:
                    proxy_secrets = (
                        runtime['proxyLink'],
                        runtime['proxyLink'].replace(
                            '{region}', runtime['site']))
                    self.fatal_error = self._safe_error(
                        exc, accounts, proxy_secrets)
            finally:
                if self.fatal_error:
                    self._clear_sensitive(runner=runner)
                with self.lock:
                    self.finished_at = time.time()
                    self.running = False

        threading.Thread(target=worker, daemon=True).start()
        return len(accounts)

    def retry_row(self, account_id):
        with self.lock:
            if self.running:
                raise RuntimeError('模块三任务正在执行')
            if not self.runner:
                raise ValueError('没有可重试的模块三任务')
            row = next((item for item in self.runner.rows
                        if item.account.account_id == account_id), None)
            if row is None:
                raise ValueError('找不到待重试账号')
            if not row.account.password:
                raise ValueError('凭证内存已清理，请重新选择原始 xlsx 后续跑')
            self._cancel_sensitive_cleanup_locked()
            self.running = True
            self.started_at = time.time()
            self.finished_at = None

        def worker():
            try:
                self.runner.retry_one(account_id)
                result_rows = self.runner.rows
                if self.ledger_enabled:
                    service = (self._ledger_service or
                               self.ledger_sync_factory())
                    self._sync_ledger_rows(
                        service, result_rows, self.runner.site,
                        self.runner.purchase_date,
                        self.runner.purchase_tag)
                with self.lock:
                    self.mapping_data = mapping_workbook_bytes(result_rows)
                    self.tsv_data = ledger_tsv_bytes(
                        result_rows, self.runner.site,
                        self.runner.purchase_date,
                        self.runner.purchase_tag)
                    self.tsv_name = ledger_tsv_filename(
                        self.runner.site, self.runner.purchase_date)
                    self.summary.update({
                        'total': len(result_rows),
                        'done': sum(row.state == 'done' for row in result_rows),
                        'failed': sum(row.state != 'done' for row in result_rows),
                    })
                self._schedule_sensitive_cleanup(self.runner)
            except Exception as exc:
                accounts = [row.account for row in self.runner.rows]
                with self.lock:
                    self.fatal_error = self._safe_error(exc, accounts)
            finally:
                with self.lock:
                    self.finished_at = time.time()
                    self.running = False

        threading.Thread(target=worker, daemon=True).start()

    def retry_ledger(self, confirm_lark_write=False):
        """Retry or supplement Feishu only; never re-run HubStudio steps."""
        if not confirm_lark_write:
            raise ValueError('飞书台账回写必须单独二次确认')
        with self.lock:
            if self.running:
                raise RuntimeError('模块三任务正在执行')
            if not self.runner:
                raise ValueError('没有可补写飞书台账的模块三任务')
            if self.ledger_sync_factory is None:
                raise ValueError('飞书 OpenAPI 回写尚未配置')
            runner = self.runner
            supplement = not self.ledger_enabled
            if supplement:
                done_rows = [row for row in runner.rows
                             if row.state == 'done']
                previous_rows = self._ledger_failure_summary(
                    done_rows, runner.site, '').get('rows') or []
                pending_ids = {
                    str(item.get('accountId') or '')
                    for item in previous_rows
                }
            else:
                previous_rows = [dict(item) for item in
                                 self.ledger_summary.get('rows') or []]
                pending_ids = {
                    str(item.get('accountId') or '')
                    for item in previous_rows
                    if item.get('state') == 'pending'
                }
                done_rows = [
                    row for row in runner.rows
                    if row.state == 'done'
                    and row.account.account_id in pending_ids
                ]
            if not done_rows:
                message = ('没有可补写的 HubStudio 成功行' if supplement
                           else '没有待重试的飞书台账行')
                raise ValueError(message)
            if any(not row.account.password for row in done_rows):
                raise ValueError('凭证内存已清理，请重新选择原始 xlsx 后续跑')
            self._cancel_sensitive_cleanup_locked()
            self.running = True
            self.started_at = time.time()
            self.finished_at = None
        try:
            service = self.ledger_sync_factory()
            if supplement:
                preflight = service.preflight_plan(
                    done_rows, runner.site, runner.purchase_tag)
                if preflight.get('conflicts'):
                    raise ValueError(
                        '飞书统一台账发现 %d 条双键或站点冲突，已阻止补写' %
                        preflight['conflicts'])
        except Exception as exc:
            error = self._safe_error(
                exc, [row.account for row in done_rows])
            with self.lock:
                self.finished_at = time.time()
                self.running = False
            self._schedule_sensitive_cleanup(runner)
            raise ValueError(error) from exc
        with self.lock:
            self._ledger_service = service
            if supplement:
                self.ledger_enabled = True
                self.ledger_summary = self._ledger_failure_summary(
                    done_rows, runner.site, '')

        def worker():
            try:
                retry_result = self._sync_ledger_rows(
                    service, done_rows, runner.site,
                    runner.purchase_date, runner.purchase_tag)
                replacements = {
                    str(item.get('accountId') or ''): dict(item)
                    for item in retry_result.get('rows') or []
                }
                merged_rows = []
                for item in previous_rows:
                    account_id = str(item.get('accountId') or '')
                    merged_rows.append(
                        replacements.pop(account_id, dict(item))
                        if account_id in pending_ids else dict(item))
                merged_rows.extend(replacements.values())
                counts = {
                    name: sum(item.get('state') == name
                              for item in merged_rows)
                    for name in ('created', 'updated', 'confirmed',
                                 'conflict', 'pending')
                }
                with self.lock:
                    self.ledger_summary = {
                        'enabled': True,
                        'running': False,
                        'total': len(merged_rows),
                        **counts,
                        'rows': merged_rows,
                        'error': retry_result.get('error') or '',
                    }
                self._schedule_sensitive_cleanup(runner)
            finally:
                with self.lock:
                    self.finished_at = time.time()
                    self.running = False

        threading.Thread(target=worker, daemon=True).start()
        return {
            'mode': 'supplement' if supplement else 'retry',
            'count': len(done_rows),
        }

    def snapshot(self):
        with self.lock:
            end_at = time.time() if self.running else self.finished_at
            elapsed = int(max(0, end_at - self.started_at)) \
                if self.started_at and end_at else 0
            ledger = {
                key: ([dict(item) for item in value]
                      if key == 'rows' else value)
                for key, value in self.ledger_summary.items()
            }
            done_rows = ([row for row in self.runner.rows
                          if row.state == 'done']
                         if self.runner else [])
            ledger['supplementAvailable'] = bool(
                not self.running
                and not self.ledger_enabled
                and self.ledger_sync_factory is not None
                and done_rows
                and all(row.account.password for row in done_rows))
            return {
                'running': self.running,
                'elapsedSec': elapsed,
                'rows': [dict(row) for row in self.rows],
                'summary': dict(self.summary),
                'ipChecks': [dict(item) for item in self.ip_checks],
                'fatalError': self.fatal_error,
                'mappingReady': self.mapping_data is not None,
                'tsvReady': self.tsv_data is not None,
                'ledger': ledger,
            }

    def mapping_export(self):
        with self.lock:
            if self.mapping_data is None:
                raise ValueError('绑定映射清单尚未生成')
            return self.mapping_data, self.mapping_name

    def tsv_export(self):
        with self.lock:
            if self.tsv_data is None:
                raise ValueError('台账 TSV 尚未生成或凭证内存已清理')
            data, name = self.tsv_data, self.tsv_name
            self.tsv_data = None
        self._clear_sensitive()
        return data, name


class BackupEnvJob(object):
    """备用/测试环境后台任务：只建环境+写备注，无凭证流转。"""

    def __init__(self, hub_getter, config_getter=load_config):
        self.hub_getter = hub_getter
        self.config_getter = config_getter
        self.lock = threading.Lock()
        self.running = False
        self.started_at = None
        self.finished_at = None
        self.rows = []
        self.summary = {}
        self.ip_checks = []
        self.fatal_error = ''
        self.result_data = None
        self.result_name = ''

    def _runtime_config(self, site='MX'):
        site = normalize_env_site(site)
        cfg = dict(self.config_getter() or {})
        try:
            workers = max(1, min(10, int(cfg.get('envCreateWorkers') or 5)))
        except (TypeError, ValueError):
            workers = 5
        return {
            'site': site,
            'purchaseTag': purchase_tag_for_site(cfg, site),
            'proxyLink': effective_proxy_link(cfg),
            'workers': workers,
        }

    @staticmethod
    def _validate_params(buyer, count, backup_type, purchase_date):
        buyer = normalize_buyer(buyer)
        count = validate_backup_count(count)
        backup_type = normalize_backup_type(backup_type)
        purchase_date = str(purchase_date or '').strip()
        if not re.fullmatch(r'20\d{6}', purchase_date):
            raise ValueError('购买日期必须是 YYYYMMDD')
        return buyer, count, backup_type, purchase_date

    def preview(self, buyer, count, backup_type, purchase_date, site='MX'):
        buyer, count, backup_type, purchase_date = self._validate_params(
            buyer, count, backup_type, purchase_date)
        runtime = self._runtime_config(site)
        hub = self.hub_getter()
        require_envbatch_ready(
            hub, runtime['purchaseTag'], runtime['proxyLink'],
            site=runtime['site'])
        existing = hub.env_list(runtime['purchaseTag'])
        names = backup_env_names(
            existing, buyer, count, backup_type, runtime['site'],
            purchase_date)
        return {
            'site': runtime['site'],
            'buyer': buyer,
            'buyerCode': BUYER_CODES[buyer],
            'type': backup_type,
            'count': len(names),
            'names': names,
            'remark': BACKUP_REMARK[backup_type],
        }

    def _set_rows(self, rows):
        with self.lock:
            self.rows = [dict(row) for row in rows]

    def start(self, buyer, count, backup_type, purchase_date,
              verify_sample_count=1, confirm_write=False, site='MX'):
        if not confirm_write:
            raise ValueError('正式执行必须二次确认 HubStudio 写入')
        try:
            verify_sample_count = max(0, int(verify_sample_count))
        except (TypeError, ValueError) as exc:
            raise ValueError('后台出口 IP 检测数量必须是非负整数') from exc
        buyer, count, backup_type, purchase_date = self._validate_params(
            buyer, count, backup_type, purchase_date)
        verify_sample_count = min(verify_sample_count, count)
        with self.lock:
            if self.running:
                raise RuntimeError('已有备用环境任务在进行')
        runtime = self._runtime_config(site)
        hub = self.hub_getter()
        # 预检先于启动线程：预检失败零写入。
        require_envbatch_ready(
            hub, runtime['purchaseTag'], runtime['proxyLink'],
            site=runtime['site'])
        with self.lock:
            if self.running:
                raise RuntimeError('已有备用环境任务在进行')
            self.running = True
            self.started_at = time.time()
            self.finished_at = None
            self.rows = []
            self.summary = {}
            self.ip_checks = []
            self.fatal_error = ''
            self.result_data = None
            self.result_name = ''

        def worker():
            try:
                runner = BackupEnvOrchestrator(
                    hub, purchase_tag=runtime['purchaseTag'],
                    proxy_link=runtime['proxyLink'], site=runtime['site'],
                    on_progress=self._set_rows,
                    max_workers=runtime['workers'])
                runner.prepare(buyer, count, backup_type, purchase_date)
                result_rows = runner.run()
                checks = runner.verify_ips(verify_sample_count)
                done = sum(row.state == 'done' for row in result_rows)
                with self.lock:
                    self.ip_checks = checks
                    self.summary = {
                        'total': len(result_rows),
                        'done': done,
                        'failed': len(result_rows) - done,
                        'ipOk': sum(bool(item.get('ok')) for item in checks),
                        'ipTotal': len(checks),
                    }
                    self.result_data = backup_result_tsv_bytes(
                        result_rows, runtime['site'])
                    self.result_name = '%s结果_%s.tsv' % (
                        BACKUP_REMARK[backup_type], purchase_date)
            except Exception as exc:
                text = str(exc)
                for secret in (runtime['proxyLink'],
                               runtime['proxyLink'].replace(
                                   '{region}', runtime['site'])):
                    if secret:
                        text = text.replace(secret, '<redacted>')
                from .redaction import scrub_text
                with self.lock:
                    self.fatal_error = scrub_text(text)[:300]
            finally:
                with self.lock:
                    self.finished_at = time.time()
                    self.running = False

        threading.Thread(target=worker, daemon=True).start()
        return count

    def snapshot(self):
        with self.lock:
            end_at = time.time() if self.running else self.finished_at
            elapsed = int(max(0, end_at - self.started_at)) \
                if self.started_at and end_at else 0
            return {
                'running': self.running,
                'elapsedSec': elapsed,
                'rows': [dict(row) for row in self.rows],
                'summary': dict(self.summary),
                'ipChecks': [dict(item) for item in self.ip_checks],
                'fatalError': self.fatal_error,
                'resultReady': self.result_data is not None,
            }

    def result_export(self):
        with self.lock:
            if self.result_data is None:
                raise ValueError('备用/测试环境结果清单尚未生成')
            return self.result_data, self.result_name


class Handler(BaseHTTPRequestHandler):

    def log_message(self, fmt, *args):
        pass   # 静默默认访问日志

    # ---- helpers ----

    def _json(self, obj, status=200, extra_headers=None):
        body = json.dumps(obj, ensure_ascii=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        for name, value in (extra_headers or {}).items():
            self.send_header(str(name), str(value))
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _extension_json(self, obj, status=200):
        origin = str(self.headers.get('Origin') or '').strip()
        headers = {'Vary': 'Origin'}
        if re.fullmatch(r'chrome-extension://[a-p]{32}', origin):
            headers['Access-Control-Allow-Origin'] = origin
        self._json(obj, status, headers)

    def _require_auth(self, path):
        if path in PUBLIC_AUTH_API_PATHS:
            return None
        if (path.startswith('/api/admin/members/')
                and path.endswith('/roles')):
            return STATE.auth.require('system.role.manage')
        if (path.startswith('/api/procurement/orders/')
                and path.endswith('/splits')):
            return STATE.auth.require('procurement.execution.manage')
        permission = AUTH_PERMISSION_BY_PATH.get(path)
        if permission is None:
            for prefix, required in AUTH_PERMISSION_BY_PREFIX:
                if path.startswith(prefix):
                    permission = required
                    break
        required_role = (
            'super_admin' if permission in SUPER_ADMIN_ONLY_PERMISSIONS
            else None)
        if required_role:
            return STATE.auth.require(permission, role=required_role)
        return STATE.auth.require(permission)

    def _auth_error(self, exc):
        self._json({
            'error': str(exc),
            'code': exc.code,
        }, exc.status)

    def _require_same_origin(self):
        """Reject browser cross-site writes to the loopback executor."""
        fetch_site = str(self.headers.get('Sec-Fetch-Site') or '').casefold()
        if fetch_site == 'cross-site':
            raise LocalAuthError(
                'origin_forbidden', '拒绝来自外部网页的本机写入请求', 403)
        origin = str(self.headers.get('Origin') or '').strip()
        if not origin:
            # Native clients and existing local scripts do not send Origin.
            return
        parsed = urlparse(origin)
        expected_host = str(self.headers.get('Host') or '').casefold()
        if (parsed.scheme != 'http' or parsed.netloc.casefold() != expected_host
                or parsed.path not in ('', '/') or parsed.query or parsed.fragment):
            raise LocalAuthError(
                'origin_forbidden', '拒绝来自外部网页的本机写入请求', 403)

    def _body(self, max_bytes=2 * 1024 * 1024):
        length = int(self.headers.get('Content-Length') or 0)
        if not length:
            return {}
        if length < 0 or length > max_bytes:
            raise ValueError('请求数据过大')
        try:
            return json.loads(self.rfile.read(length).decode('utf-8'))
        except Exception:
            return {}

    def _loopback_base_url(self):
        host = str(self.headers.get('Host') or '').strip().lower()
        if not re.fullmatch(r'127\.0\.0\.1:\d{1,5}', host):
            raise ExtensionBridgeError(
                'extension_host_invalid', 'Xynigo 本机服务地址无效', 400)
        port = int(host.rsplit(':', 1)[1])
        if port < 1 or port > 65535:
            raise ExtensionBridgeError(
                'extension_host_invalid', 'Xynigo 本机服务端口无效', 400)
        return 'http://' + host

    def _handle_extension_post(self, path, body):
        if not isinstance(body, dict):
            return self._extension_json({
                'ok': False,
                'code': 'extension_request_invalid',
                'error': '插件请求格式无效',
            }, 400)
        origin = str(self.headers.get('Origin') or '').strip()
        client_id = body.get('clientId')
        if path == '/api/extension/v1/pair/request':
            result = STATE.extension_bridge.request_pairing(
                client_id,
                body.get('clientVersion'),
                origin,
            )
            base_url = self._loopback_base_url()
            approval_url = '%s/extension-connect?clientId=%s' % (
                base_url, result['clientId'])
            return self._extension_json({
                'ok': True,
                'service': 'xynigo-sourcing',
                'apiVersion': 1,
                'status': 'approval-required',
                'approvalUrl': approval_url,
                **result,
            }, 202)

        STATE.extension_bridge.authenticate(
            client_id,
            body.get('bridgeToken') if isinstance(body, dict) else None,
            origin,
        )
        if path == '/api/extension/v1/status':
            auth_state = STATE.auth.status(force=True)
            return self._extension_json({
                'ok': True,
                'service': 'xynigo-sourcing',
                'apiVersion': 1,
                'authenticated': bool(auth_state.get('authenticated')),
                'identity': auth_state.get('identity'),
                'code': auth_state.get('code') or '',
                'message': auth_state.get('message') or '',
            })
        actions = {
            '/api/extension/v1/purchase-orders/draft': (
                'draft', 'procurement.request.save', body.get('draft')),
            '/api/extension/v1/purchase-orders/submit': (
                'submit', 'procurement.request.submit', body.get('draft')),
            '/api/extension/v1/purchase-orders/get': (
                'get', 'procurement.request.read',
                {'orderKey': body.get('orderKey')}),
        }
        action = actions.get(path)
        if action is None:
            return self._extension_json({
                'ok': False,
                'code': 'not_found',
                'error': '接口不存在',
            }, 404)
        cloud_action, permission, payload = action
        if not isinstance(payload, dict):
            return self._extension_json({
                'ok': False,
                'code': 'purchase_payload_invalid',
                'error': '采购单数据无效',
            }, 400)
        result = STATE.auth.purchase_request(
            cloud_action, payload, permission)
        return self._extension_json({
            'ok': True,
            'data': result['data'],
        })

    def _file(self, path, mime):
        with open(path, 'rb') as f:
            body = f.read()
        self.send_response(200)
        content_type = (mime + '; charset=utf-8'
                        if mime.startswith('text/') else mime)
        self.send_header('Content-Type', content_type)
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _download(self, data, name, mime):
        self.send_response(200)
        self.send_header('Content-Type', mime)
        self.send_header('Cache-Control', 'no-store')
        self.send_header(
            'Content-Disposition',
            'attachment; filename*=UTF-8\'\'%s' % quote(name))
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _redirect(self, location):
        self.send_response(302)
        self.send_header('Location', location)
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Content-Length', '0')
        self.end_headers()

    # ---- GET ----

    def do_GET(self):
        parsed = urlparse(self.path)
        path, query = parsed.path, parse_qs(parsed.query)
        try:
            if path.startswith('/api/'):
                self._require_auth(path)
            if path == '/':
                self._file(INDEX_HTML, 'text/html')
            elif path == '/xynigo-logo.png':
                self._file(LOGO_PNG, 'image/png')
            elif path == '/xynigo-mascot-x.png':
                self._file(MASCOT_X_PNG, 'image/png')
            elif path == '/xynigo-x.png':
                self._file(X_ICON_PNG, 'image/png')
            elif path == '/xynigo-x.ico':
                self._file(X_ICON_ICO, 'image/x-icon')
            elif path == '/favicon.ico':
                self._file(X_ICON_ICO, 'image/x-icon')
            elif path == '/extension-connect':
                self._file(EXTENSION_CONNECT_HTML, 'text/html')
            elif path == '/extension-connect.js':
                self._file(EXTENSION_CONNECT_JS, 'text/javascript')
            elif path == '/api/auth/status':
                self._json(STATE.auth.status(force=True))
            elif path == '/api/hub-status':
                ok, err = STATE.hub_status(force=True)
                self._json({'connected': ok, 'error': err})
            elif path == '/api/update/status':
                STATE.updates.check_async()
                self._json(STATE.updates.snapshot())
            elif path == '/api/groups':
                self._json({'groups': STATE.hub.group_list()})
            elif path == '/api/group-envs':
                group = (query.get('group') or [''])[0]
                envs = STATE.hub.env_list(group or None)
                serials = sorted(
                    [str(e.get('serialNumber')) for e in envs
                     if e.get('serialNumber') is not None],
                    key=lambda x: int(x) if x.isdigit() else 0)
                self._json({'serials': serials, 'count': len(serials)})
            elif path == '/api/progress':
                snap = STATE.orch.snapshot()
                snap['hubConnected'] = STATE.hub_status()[0]
                self._json(snap)
            elif path == '/api/register/progress':
                snap = STATE.reg_job.snapshot()
                snap['hubConnected'] = STATE.hub_status()[0]
                self._json(snap)
            elif path == '/api/buyer-library':
                self._json(STATE.buyer_library.list_public(
                    site=(query.get('site') or [''])[0],
                    status=(query.get('status') or [''])[0],
                    limit=(query.get('limit') or ['100'])[0]))
            elif path == '/api/resources/stores':
                self._json(STATE.resources.stores_snapshot(
                    force=(query.get('refresh') or ['0'])[0] == '1'))
            elif path == '/api/resources/stores/export':
                self._download(
                    STATE.resources.store_export(),
                    'Xynigo店铺环境对账.csv',
                    'text/csv; charset=utf-8')
            elif path == '/api/resources/proxies':
                self._json(STATE.resources.proxies_snapshot(
                    force=(query.get('refresh') or ['0'])[0] == '1'))
            elif path == '/api/resources/proxies/export':
                self._download(
                    STATE.resources.proxy_export(),
                    'Xynigo代理IP健康清单.csv',
                    'text/csv; charset=utf-8')
            elif path == '/api/resources/proxies/check/progress':
                self._json(STATE.resources.proxy_check_snapshot())
            elif path == '/api/resources/proxies/check/history':
                self._json(STATE.resources.proxy_history(
                    (query.get('assetId') or [''])[0]))
            elif path == '/api/envbatch/progress':
                snap = STATE.env_job.snapshot()
                snap['hubConnected'] = STATE.hub_status()[0]
                self._json(snap)
            elif path == '/api/envbatch/preflight':
                site = (query.get('site') or ['MX'])[0]
                self._json(STATE.env_job.preflight(site))
            elif path == '/api/envbatch/template':
                if not os.path.isfile(ENV_TEMPLATE_XLSX):
                    return self._json({'error': '填写模板未打包'}, 404)
                with open(ENV_TEMPLATE_XLSX, 'rb') as handle:
                    data = handle.read()
                self._download(
                    data, '采购工具买家号入库模板.xlsx',
                    'application/vnd.openxmlformats-officedocument.'
                    'spreadsheetml.sheet')
            elif path == '/api/lark/template':
                if not os.path.isfile(LARK_LEDGER_TEMPLATE_XLSX):
                    return self._json({'error': '统一台账模板未打包'}, 404)
                with open(LARK_LEDGER_TEMPLATE_XLSX, 'rb') as handle:
                    data = handle.read()
                self._download(
                    data, '买家号统一台账模板.xlsx',
                    'application/vnd.openxmlformats-officedocument.'
                    'spreadsheetml.sheet')
            elif path == '/api/envbatch/export-mapping':
                data, name = STATE.env_job.mapping_export()
                self._download(
                    data, name,
                    'application/vnd.openxmlformats-officedocument.'
                    'spreadsheetml.sheet')
            elif path == '/api/envbatch/export-tsv':
                data, name = STATE.env_job.tsv_export()
                self._download(data, name, 'text/tab-separated-values; charset=utf-8')
            elif path == '/api/envbatch/backup/preview':
                result = STATE.backup_job.preview(
                    (query.get('buyer') or [''])[0],
                    (query.get('count') or [''])[0],
                    (query.get('type') or [''])[0],
                    (query.get('purchaseDate') or
                     [time.strftime('%Y%m%d')])[0],
                    site=(query.get('site') or ['MX'])[0])
                self._json(result)
            elif path == '/api/envbatch/backup/progress':
                snap = STATE.backup_job.snapshot()
                snap['hubConnected'] = STATE.hub_status()[0]
                self._json(snap)
            elif path == '/api/envbatch/backup/result':
                data, name = STATE.backup_job.result_export()
                self._download(data, name,
                               'text/tab-separated-values; charset=utf-8')
            elif path == '/api/export':
                fmt = (query.get('format') or ['xlsx'])[0]
                rows = STATE.orch.snapshot()['rows']
                if not rows:
                    return self._json({'error': '还没有查询结果'}, 400)
                data, name, mime = export_bytes(
                    rows, fmt, STATE.orch.screenshot_bytes)
                self.send_response(200)
                self.send_header('Content-Type', mime)
                self.send_header(
                    'Content-Disposition',
                    'attachment; filename*=UTF-8\'\'%s' % quote(name))
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            elif path == '/api/screenshot':
                serial = (query.get('serial') or [''])[0]
                data = STATE.orch.screenshot_bytes(serial)
                if not data:
                    return self._json({'error': '截图不存在或已清理'}, 404)
                self.send_response(200)
                self.send_header('Content-Type', 'image/jpeg')
                self.send_header('Cache-Control', 'no-store')
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            elif path == '/api/config':
                self._json(public_config(STATE.cfg))
            elif path == '/api/lark/status':
                self._json(public_lark_runtime_status(
                    STATE.cfg, STATE.lark_credentials))
            elif path == '/api/lark/config':
                self._json(public_lark_config(
                    STATE.cfg, STATE.lark_credentials))
            elif path == '/api/lark/open-target':
                self._redirect(lark_target_link(STATE.cfg))
            elif path.startswith('/api/procurement/'):
                cloud_path = '/v1/procurement/' + path[len('/api/procurement/'):]
                if parsed.query:
                    cloud_path += '?' + parsed.query
                self._json(STATE.auth.procurement_workspace_request(cloud_path))
            elif path.startswith('/api/admin/'):
                cloud_path = '/v1/admin/' + path[len('/api/admin/'):]
                if parsed.query:
                    cloud_path += '?' + parsed.query
                self._json(STATE.auth.admin_request(cloud_path))
            else:
                self._json({'error': 'not found'}, 404)
        except LocalAuthError as e:
            if path.startswith('/api/extension/v1/'):
                self._extension_json({
                    'ok': False,
                    'code': e.code,
                    'error': str(e),
                }, e.status)
            else:
                self._auth_error(e)
        except ConnectionError as e:
            self._json({'error': 'HubStudio 未连接：%s' % e}, 503)
        except ValueError as e:
            self._json({'error': str(e)}, 400)
        except Exception as e:
            self._json({'error': public_error(e)}, 500)

    # ---- POST ----

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            body = self._body()
            if path.startswith('/api/extension/v1/'):
                return self._handle_extension_post(path, body)
            if path.startswith('/api/'):
                self._require_same_origin()
                self._require_auth(path)
            if path == '/api/auth/start':
                self._json(STATE.auth.start_login(), 201)
            elif path == '/api/auth/poll':
                self._json(STATE.auth.poll_login())
            elif path == '/api/auth/logout':
                self._json(STATE.auth.logout())
            elif path == '/api/extension/pair/approve':
                approved = STATE.extension_bridge.approve(body.get('clientId'))
                self._json({
                    'ok': True,
                    'apiBaseUrl': self._loopback_base_url(),
                    **approved,
                })
            elif path.startswith('/api/admin/'):
                cloud_path, cloud_method = admin_cloud_write_target(path)
                self._json(STATE.auth.admin_request(
                    cloud_path, method=cloud_method, payload=body))
            elif (path == '/api/procurement/claims'
                    or path.startswith('/api/procurement/orders/')
                    and path.endswith('/splits')):
                cloud_path = '/v1/procurement/' + path[len('/api/procurement/'):]
                self._json(STATE.auth.procurement_workspace_request(
                    cloud_path,
                    method='POST',
                    payload=body,
                    permission='procurement.execution.manage'))
            elif path == '/api/query':
                if STATE.orch.running:
                    return self._json({'error': '已有查询在进行中'}, 409)
                if STATE.env_job.running or STATE.backup_job.running:
                    return self._json({'error': '模块三建环境正在进行'}, 409)
                serials = body.get('serials')
                group = body.get('group')
                site = normalize_site(body.get('site') or 'MX')
                env_index = None
                if not serials and group:
                    envs = STATE.hub.env_list(group)
                    serials = sorted(
                        [str(e.get('serialNumber')) for e in envs
                         if e.get('serialNumber') is not None],
                        key=lambda x: int(x) if x.isdigit() else 0)
                    env_index = {str(e.get('serialNumber')): e
                                 for e in envs}
                if not serials:
                    return self._json({'error': '未提供环境序号'}, 400)
                STATE.orch.start_batch(serials, env_index, site=site)
                self._json({'started': True, 'total': len(serials),
                            'site': site})
            elif path == '/api/stop':
                STATE.orch.request_stop()
                self._json({'stopped': True})
            elif path == '/api/requery':
                serial = str(body.get('serial') or '')
                if not serial:
                    return self._json({'error': '缺少 serial'}, 400)
                STATE.orch.requery(serial, force=bool(body.get('force')))
                self._json({'started': True})
            elif path == '/api/requery-failed':
                count = STATE.orch.requery_failed()
                self._json({'started': True, 'count': count})
            elif path == '/api/update/check':
                started = STATE.updates.check_async(force=True)
                payload = STATE.updates.snapshot()
                payload['started'] = started
                self._json(payload, 202 if started else 200)
            elif path == '/api/update/prompt':
                accepted = STATE.updates.prompt_async()
                payload = STATE.updates.snapshot()
                payload['accepted'] = accepted
                if not accepted:
                    payload['error'] = '当前没有可确认的新版本'
                    return self._json(payload, 409)
                self._json(payload, 202)
            elif path == '/api/register/validate':
                plan = STATE.reg_job.validate(body.get('tasks'))
                self._json({'valid': True, 'count': len(plan),
                            'plan': plan})
            elif path == '/api/register/start':
                if STATE.orch.running:
                    return self._json({
                        'error': '物流查询正在进行，请结束后再注册'}, 409)
                if STATE.env_job.running or STATE.backup_job.running:
                    return self._json({
                        'error': '模块三建环境正在进行，请结束后再注册'}, 409)
                count = STATE.reg_job.start(
                    body.get('tasks'),
                    accept_terms=bool(body.get('acceptTerms')),
                    acknowledge_ms_privacy=bool(
                        body.get('acknowledgeMsPrivacy')),
                    keep_open=bool(body.get('keepOpen')),
                    write_lark_ledger=bool(body.get('writeLarkLedger')),
                    confirm_lark_write=bool(body.get('confirmLarkWrite')))
                self._json({'started': True, 'count': count})
            elif path == '/api/buyer-library/import/parse':
                result = STATE.buyer_library.parse(
                    body.get('filename'), body.get('contentBase64'),
                    body.get('site') or 'MX', body.get('vendorName'),
                    body.get('batchNo'),
                    body.get('purchaseDate') or time.strftime('%Y-%m-%d'))
                self._json(result)
            elif path == '/api/buyer-library/import/commit':
                result = STATE.buyer_library.commit(
                    body.get('planId'),
                    confirm_write=bool(body.get('confirmWrite')))
                self._json({'saved': True, **result})
            elif path == '/api/resources/proxies/check/start':
                count = STATE.resources.start_proxy_checks(
                    body.get('assetIds'),
                    concurrency=body.get('concurrency', 10),
                    timeout=body.get('timeoutSec', 8))
                self._json({'started': True, 'count': count}, 202)
            elif path == '/api/resources/proxies/check/stop':
                STATE.resources.stop_proxy_checks()
                self._json({'stopping': True})
            elif path == '/api/envbatch/parse':
                result = STATE.env_job.parse(
                    body.get('filename'), body.get('contentBase64'))
                self._json(result)
            elif path == '/api/envbatch/preview':
                rows = STATE.env_job.preview(
                    body.get('planId'), body.get('assignment'),
                    body.get('purchaseDate') or time.strftime('%Y%m%d'),
                    site=body.get('site') or 'MX')
                self._json({'valid': True, 'count': len(rows), 'rows': rows})
            elif path == '/api/envbatch/start':
                if (STATE.orch.running or STATE.reg_job.running
                        or STATE.backup_job.running):
                    return self._json({
                        'error': '模块一/二或备用环境任务正在进行，请结束后再建环境'}, 409)
                count = STATE.env_job.start(
                    body.get('planId'), body.get('assignment'),
                    body.get('purchaseDate') or time.strftime('%Y%m%d'),
                    verify_sample_count=body.get('verifySampleCount', 3),
                    confirm_write=bool(body.get('confirmWrite')),
                    site=body.get('site') or 'MX',
                    write_lark_ledger=bool(body.get('writeLarkLedger')),
                    confirm_lark_write=bool(body.get('confirmLarkWrite')))
                self._json({'started': True, 'count': count})
            elif path == '/api/envbatch/retry-row':
                STATE.env_job.retry_row(str(body.get('accountId') or ''))
                self._json({'started': True})
            elif path == '/api/envbatch/retry-ledger':
                result = STATE.env_job.retry_ledger(
                    confirm_lark_write=bool(body.get('confirmLarkWrite')))
                self._json({'started': True, **result})
            elif path == '/api/envbatch/backup/start':
                if (STATE.orch.running or STATE.reg_job.running
                        or STATE.env_job.running):
                    return self._json({
                        'error': '模块一/二/三任务正在进行，请结束后再执行'}, 409)
                count = STATE.backup_job.start(
                    body.get('buyer'), body.get('count'), body.get('type'),
                    body.get('purchaseDate') or time.strftime('%Y%m%d'),
                    verify_sample_count=body.get('verifySampleCount', 1),
                    confirm_write=bool(body.get('confirmWrite')),
                    site=body.get('site') or 'MX')
                self._json({'started': True, 'count': count})
            elif path == '/api/config':
                old_cfg = load_config()
                cfg = updated_config(old_cfg, body)
                save_config(cfg)
                STATE.cfg = cfg
                reconnect_needed = (
                    cfg.get('hubPort') != old_cfg.get('hubPort')
                    or cfg.get('concurrency') != old_cfg.get('concurrency'))
                connected = (STATE.reconnect_hub() if reconnect_needed
                             else STATE.hub_status()[0])
                self._json({'saved': True, 'hubConnected': connected})
            elif path == '/api/lark/config':
                credentials = submitted_lark_credentials(body)
                resolved_target = resolve_submitted_lark_target(
                    body, STATE.lark_credentials)
                cfg = updated_lark_config(
                    load_config(), body, resolved_target)
                target_validation_error = ''
                if body.get('clearCredential'):
                    STATE.lark_credentials.clear()
                elif credentials:
                    STATE.lark_credentials.save(
                        credentials.app_id, credentials.app_secret)
                if (str(cfg.get('larkBuyerBaseToken') or '').strip()
                        and str(cfg.get('larkBuyerTableId') or '').strip()
                        and not body.get('clearCredential')):
                    try:
                        cfg = refreshed_lark_target_labels(
                            cfg, STATE.lark_credentials)
                    except Exception as exc:
                        cfg['larkBuyerTargetVerified'] = False
                        target_validation_error = public_error(exc)
                save_config(cfg)
                STATE.cfg = cfg
                response = {
                    'saved': True,
                    **public_lark_config(cfg, STATE.lark_credentials),
                }
                if target_validation_error:
                    response['targetValidationError'] = \
                        target_validation_error
                self._json(response)
            elif path == '/api/lark/target-metadata':
                cfg = refreshed_lark_target_labels(
                    STATE.cfg, STATE.lark_credentials)
                save_config(cfg)
                STATE.cfg = cfg
                self._json({
                    'refreshed': True,
                    **public_lark_config(cfg, STATE.lark_credentials),
                })
            elif path == '/api/lark/preflight':
                service = build_buyer_ledger_service(
                    STATE.cfg, STATE.lark_credentials)
                cfg = refreshed_lark_target_labels(
                    STATE.cfg, STATE.lark_credentials,
                    client=service.client)
                save_config(cfg)
                STATE.cfg = cfg
                validate_unified_schema(service.client.list_fields())
                self._json({
                    'ready': True,
                    'message': '飞书统一台账字段检查通过',
                    **public_lark_config(cfg, STATE.lark_credentials),
                })
            else:
                self._json({'error': 'not found'}, 404)
        except LocalAuthError as e:
            if path.startswith('/api/extension/v1/'):
                self._extension_json({
                    'ok': False,
                    'code': e.code,
                    'error': str(e),
                }, e.status)
            else:
                self._auth_error(e)
        except ExtensionBridgeError as e:
            if path.startswith('/api/extension/v1/'):
                self._extension_json({
                    'ok': False,
                    'code': e.code,
                    'error': str(e),
                }, e.status)
            else:
                self._json({
                    'ok': False,
                    'code': e.code,
                    'error': str(e),
                }, e.status)
        except RuntimeError as e:
            self._json({'error': str(e)}, 409)
        except ValueError as e:
            self._json({'error': str(e)}, 400)
        except ConnectionError as e:
            self._json({'error': 'HubStudio 未连接：%s' % e}, 503)
        except Exception as e:
            self._json({'error': public_error(e)}, 500)


def quote(name):
    from urllib.parse import quote as _q
    return _q(name)


def main(argv=None):
    global STATE
    argv = argv or sys.argv[1:]
    port = load_config()['serverPort']
    no_browser = '--no-browser' in argv
    STATE = build_state()
    server = None
    for p in [port + i for i in range(10)]:
        try:
            server = ThreadingHTTPServer(('127.0.0.1', int(p)), Handler)
            port = p
            break
        except OSError:
            continue
    if server is None:
        print('端口 %s-%s 均被占用，退出' % (port, port + 9))
        sys.exit(1)
    url = 'http://127.0.0.1:%s' % port
    print('Xynigo Sourcing v%s  服务运行中：%s' % (__version__, url))
    print('保持此窗口开启；关闭窗口即退出工具。')
    ok, err = STATE.hub_status(force=True)
    if ok:
        print('HubStudio 连接：正常')
    else:
        print('HubStudio 连接：失败 —— %s' % err)
    STATE.updates.check_async()
    if not no_browser:
        def _open():
            ok = False
            try:
                ok = webbrowser.open(url)
            except Exception:
                pass
            if not ok and hasattr(os, 'startfile'):   # Windows 兜底
                try:
                    os.startfile(url)
                    ok = True
                except Exception:
                    pass
            if not ok:
                print('浏览器未能自动打开，请手动访问：%s' % url)
        threading.Timer(0.6, _open).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
