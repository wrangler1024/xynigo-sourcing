# -*- coding: utf-8 -*-
"""采购工具服务入口：标准库 HTTP 服务 + 自动打开浏览器。

启动：python -m purchase_tool   （或打包后的 exe / 启动脚本）
API：
  GET  /                    操作页面
  GET  /api/hub-status      探测 HubStudio 是否在线
  GET  /executor-status.json 托盘状态中心读取本机安全状态
  GET  /api/groups          分组列表
  GET  /api/group-envs      指定分组的环境序号（查全部分组用）
  POST /api/query           {serials:[...],site:"MX|US"} 或 {group:"分组名",site}
  GET  /api/progress        查询进度与结果行
  POST /api/stop            停止当前批次
  POST /api/requery         {serial} 单行重查
  POST /api/register/validate 脱敏校验注册凭证文件
  POST /api/register/start  启动低并发注册
  GET  /api/register/progress 注册脱敏进度
  GET  /api/buyer-library 从 PostgreSQL 读取买家号脱敏元数据
  POST /api/buyer-library/import/parse 号商 xlsx 入库预检
  POST /api/buyer-library/import/commit 二次确认后写入买家号库
  POST /api/assistant/procurement-import/parse 店小秘 XYP2 本地解析
  GET  /api/assistant/procurement-import/image 预览订单商品图片
  GET  /api/assistant/procurement-import/export 下载采购共享协作表
  POST /api/assistant/procurement-import/target/inspect 读取普通飞书工作簿
  POST /api/assistant/procurement-import/target/validate 校验 A:AH 表头
  POST /api/assistant/procurement-import/sheet-sync 追加 A:AH 数据、样式、链接并补齐 M 列图片
  GET  /api/assistant/procurement-import/sheet-sync/status 查询导入进度
  POST /api/envbatch/parse  模块三 xlsx 严格解析（只返回脱敏计划）
  POST /api/envbatch/preview/start/retry-row/retry-failed 模块三预览/执行/重试
  GET  /api/envbatch/progress/export-mapping 模块三进度/安全映射导出
  GET  /api/export          ?format=xlsx|csv 下载结果
  GET/POST /api/config      本机配置（HubStudio 端口等，存 config.json）
"""
import base64
import binascii
import copy
import csv
from io import BytesIO, StringIO
import json
import os
import re
import secrets
import sys
import threading
import time
import webbrowser
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, unquote

from . import __version__
from .buyer_library import BuyerLibraryJob, DatabaseBuyerLibraryService
from .buyer_ledger_sync import validate_unified_schema
from .buyer_register import BuyerRegistrationTask, RegistrationOrchestrator
from .cloud_auth import DEFAULT_AUTH_BASE_URL, LocalAuthError, LocalAuthService
from .cloud_feishu_transport import CloudFeishuTransport
from .data_source_registry import (
    DataSourceMappingRequired, DataSourceRegistry, DataSourceRegistryError,
    runtime_config_for_source)
from .excel_export import EXPORT_HEAD, export_bytes
from .env_batch import (BACKUP_MAX_COUNT, BACKUP_REMARK, BUYER_CODES,
                        BUYER_ROSTER, BatchEnvOrchestrator,
                        BackupEnvOrchestrator, DEFAULT_PROXY_LINK,
                        DEFAULT_SPLIT_BUYERS,
                        EnvBatchError,
                        ResumeStateStore, backup_env_names,
                        backup_result_tsv_bytes,
                        batch_fingerprint, build_batch_plan,
                        build_environment_inventory_snapshot,
                        count_mixed_site_accounts,
                        deserialize_buyer_accounts,
                        envbatch_preflight,
                        mapping_workbook_bytes, normalize_backup_type,
                        normalize_buyer, parse_assignment,
                        normalize_env_site,
                        parse_vendor_workbook, require_envbatch_ready,
                        validate_accounts_site,
                        validate_assignment_template,
                        validate_backup_count,
                        validate_proxy_link, validate_purchase_group_site,
                        validate_purchase_tag)
from .executor_channel import (
    CloudExecutorClient, ExecutorChannelStateStore, ExecutorChannelWorker,
    LOCAL_CONFIG_RPC_PATHS, config_revision,
    system_executor_credential_store)
from .operation_executor import LocalOperationExecutor, backup_account_ref
from .extension_bridge import ExtensionBridge, ExtensionBridgeError
from .hub_api import HubApiError, HubStudioApi, DEFAULT_PORT
from .hub_core_repair import HubCoreRepairCoordinator, HubCoreRepairError
from .hub_api_key import (
    HubApiKeyStoreError, public_hub_api_key_status,
    system_hub_api_key_store)
from .instance_guard import acquire_executor_instance_guard
from .lark_credentials import (LarkCredentialError, LarkCredentials,
                               public_credential_status,
                               system_credential_store)
from .lark_links import (LarkLedgerTargetConfig, build_lark_base_link,
                         parse_lark_base_link, resolve_lark_ledger_link)
from .lark_openapi import LarkOpenApiClient
from .lark_runtime import build_buyer_ledger_service
from .local_config_service import (
    LocalConfigRevisionConflict, LocalConfigService)
from .operation_result_sync import OperationResultSyncQueue
from .procurement_import import ProcurementImportService
from .purchase_assistant import (
    PurchaseAssistantError, PurchaseAssistantService)
from .redaction import scrub_text
from .resource_center import ResourceCenterService
from .secure_store_transaction import SecureStoreTransaction
from .shein_query import (
    QueryOrchestrator, normalize_browser_mode, normalize_site)
from .task_runtime import (HubRuntimeGate, LocalTaskCoordinator, TaskConflict,
                           environment_resources)
from .updater import StandardInstallerUpdateClient, UpdateCoordinator
from .workspace_rpc import WorkspaceRpcClient

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
DESKTOP_HTML = os.path.join(BASE_DIR, 'web', 'desktop.html')
DESKTOP_CSS = os.path.join(BASE_DIR, 'web', 'desktop.css')
DESKTOP_JS = os.path.join(BASE_DIR, 'web', 'desktop.js')
ENV_TEMPLATE_XLSX = os.path.join(
    BASE_DIR, 'web', '采购工具买家号入库模板.xlsx')
LARK_LEDGER_TEMPLATE_XLSX = os.path.join(
    BASE_DIR, 'web', '买家号统一台账模板.xlsx')
DATA_DIR = os.path.abspath(
    os.environ.get('XYNIGO_DATA_DIR') or os.getcwd())
CONFIG_PATH = os.path.join(DATA_DIR, 'config.json')
LOCAL_BINDINGS_PATH = os.path.join(
    DATA_DIR, '运行数据', 'local-bindings-v1.json')
LOG_DIR = os.path.join(DATA_DIR, '查询日志')
HUB_CORE_AUDIT_PATH = os.path.join(LOG_DIR, 'hub-core-repair-audit.jsonl')
PURCHASE_ASSISTANT_API_PREFIX = '/api/purchase-assistant/v1'
DATA_SOURCE_API_PREFIX = '/api/local-config/data-sources'


def editable_data_source(identity, source_id, *, allow_unclaimed=False):
    """Resolve a source and enforce its desktop editing boundary."""
    source = STATE.data_sources.source(source_id)
    member_id = str((identity.get('user') or {}).get('id') or '')
    roles = set(identity.get('roles') or [])
    admin = bool(roles & {'admin', 'super_admin'})
    if source['scope'] == 'team' and not admin:
        raise LocalAuthError('permission_denied', status=403)
    owner = str(source.get('ownerMemberId') or '')
    if (source['scope'] == 'personal'
            and owner not in {member_id}
            and not (allow_unclaimed and not owner)
            and not admin):
        raise LocalAuthError('permission_denied', status=403)
    return source


def public_purchase_assistant_source_context(identity, source_status,
                                             container_code=''):
    """Attach only safe desktop-management and resolution context."""
    payload = copy.deepcopy(source_status if isinstance(
        source_status, dict) else {})
    resolution = str(payload.get('resolution') or '')
    payload.update({
        'management': 'desktop',
        'settingsUrl': 'xynigo://settings',
        'member': {
            'name': str(((identity or {}).get('user') or {}).get(
                'name') or '')[:255],
        },
        'containerContextApplied': bool(
            str(container_code or '').strip()
            and resolution == 'environment_binding'),
    })
    return payload


CONFIG_FIELDS = frozenset({
    'hubPort', 'serverPort', 'concurrency', 'importBuyerPlan',
    'verifySampleCount', 'hiddenQueryColumns', 'purchaseSite',
    'purchaseTag', 'purchaseTags', 'proxyLink', 'envCreateWorkers',
    'safeParallelTasks', 'queryBrowserMode',
    'larkBuyerBaseToken', 'larkBuyerTableId',
    'larkBuyerTargetHost',
    'larkBuyerBaseName', 'larkBuyerTableName',
    'larkBuyerTargetVerified',
    'purchaseAssistantSpreadsheetToken', 'purchaseAssistantSheetId',
    'purchaseAssistantCellRange', 'purchaseAssistantApiBase',
    'purchaseAssistantCacheTtlSeconds',
    'purchaseAssistantSourceMode',
    'purchaseAssistantPersonalSpreadsheetToken',
    'purchaseAssistantPersonalSheetId',
    'purchaseAssistantPersonalCellRange',
    'purchaseAssistantPersonalSheetName',
    'purchaseAssistantTeamSpreadsheetToken',
    'purchaseAssistantTeamSheetId',
    'purchaseAssistantTeamCellRange',
    'purchaseAssistantTeamSheetName',
})
CONFIG_REQUEST_FIELDS = (CONFIG_FIELDS - {
    'larkBuyerBaseToken', 'larkBuyerTableId',
    'larkBuyerTargetHost',
    'larkBuyerBaseName', 'larkBuyerTableName',
    'larkBuyerTargetVerified',
    'purchaseAssistantSpreadsheetToken', 'purchaseAssistantSheetId',
    'purchaseAssistantCellRange', 'purchaseAssistantApiBase',
    'purchaseAssistantCacheTtlSeconds',
    'purchaseAssistantSourceMode',
    'purchaseAssistantPersonalSpreadsheetToken',
    'purchaseAssistantPersonalSheetId',
    'purchaseAssistantPersonalCellRange',
    'purchaseAssistantPersonalSheetName',
    'purchaseAssistantTeamSpreadsheetToken',
    'purchaseAssistantTeamSheetId',
    'purchaseAssistantTeamCellRange',
    'purchaseAssistantTeamSheetName'}) | {'proxyClear'}

# Cloud device configuration is intentionally narrower than the legacy local
# config.json contract. Business choices belong to each environment/purchase
# task, not to a machine-wide executor profile.
EXECUTOR_RUNTIME_CONFIG_FIELDS = (
    'hubPort',
    'concurrency',
    'envCreateWorkers',
    'verifySampleCount',
    'safeParallelTasks',
    'queryBrowserMode',
)

_LOCAL_CONFIG_SERVICES = {}
_LOCAL_CONFIG_SERVICES_LOCK = threading.Lock()

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
    '/api/cloud/buyer-accounts': 'resource.buyer.read',
    '/api/cloud/buyer-accounts/snapshot': 'resource.buyer.import',
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
    '/api/lark/target-url': 'system.lark_connection.manage',
    '/api/lark/target-metadata': 'system.lark_connection.manage',
    '/api/lark/preflight': 'system.lark_connection.manage',
    '/api/hub-api-key': 'system.integration.manage',
    '/api/hub-core-repair/status': 'system.integration.manage',
    '/api/hub-core-repair/start': 'system.integration.manage',
    '/api/extension/pair/approve': 'operations.access',
    '/api/procurement/claims': 'procurement.execution.manage',
    '/api/assistant/procurement-import/parse': 'assistant.access',
    '/api/assistant/procurement-import/image': 'assistant.access',
    '/api/assistant/procurement-import/export': 'assistant.access',
    '/api/assistant/procurement-import/target/inspect': 'assistant.access',
    '/api/assistant/procurement-import/target/validate': 'assistant.access',
    '/api/assistant/procurement-import/image-sync': 'assistant.access',
    '/api/assistant/procurement-import/image-sync/status': 'assistant.access',
    '/api/assistant/procurement-import/sheet-sync': 'assistant.access',
    '/api/assistant/procurement-import/sheet-sync/status': 'assistant.access',
}
SUPER_ADMIN_ONLY_PERMISSIONS = frozenset({
    'system.lark_connection.manage',
    'system.integration.manage',
    'resource.ip.credential.manage',
})
AUTH_PERMISSION_BY_PREFIX = (
    ('/api/procurement/', 'procurement.request.read'),
    ('/api/system-logs', 'system.runtime_log.read'),
    ('/api/admin/roles', 'system.role.manage'),
    ('/api/admin/permissions', 'system.role.manage'),
    ('/api/admin/sessions', 'system.member.manage'),
    ('/api/admin/members', 'system.member.manage'),
    ('/api/envbatch/', 'resource.environment.create'),
)


def procurement_write_path(path):
    """Return whether a same-origin route is an approved procurement mutation."""
    uuid_part = (
        r'[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-'
        r'[0-9a-fA-F]{4}-[0-9a-fA-F]{12}'
    )
    return (
        path == '/api/procurement/claims'
        or bool(re.fullmatch(
            r'/api/procurement/orders/' + uuid_part
            + r'/(?:splits|return|checkout-attempts)', path))
        or bool(re.fullmatch(
            r'/api/procurement/checkout-attempts/' + uuid_part
            + r'/(?:revise|begin|abandon|payment-result|cleanup-result)', path))
        or bool(re.fullmatch(
            r'/api/procurement/purchase-batches/' + uuid_part + r'/shipments',
            path))
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
    assistant_token = os.environ.get(
        'XYNIGO_PURCHASE_ASSISTANT_SPREADSHEET_TOKEN', '')
    assistant_sheet_id = os.environ.get(
        'XYNIGO_PURCHASE_ASSISTANT_SHEET_ID', '')
    assistant_cell_range = os.environ.get(
        'XYNIGO_PURCHASE_ASSISTANT_CELL_RANGE', 'A1:AQ')
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
        'safeParallelTasks': True,
        'queryBrowserMode': 'headless',
        # Base/table identifiers are local routing configuration.  The App
        # Secret lives in Keychain/DPAPI and is never written to config.json.
        'larkBuyerBaseToken': os.environ.get('XYNIGO_LARK_BASE_TOKEN', ''),
        'larkBuyerTableId': (os.environ.get('XYNIGO_LARK_TABLE_ID') or
                             os.environ.get('XYNIGO_LARK_TABLE_ID_MX', '')),
        'larkBuyerTargetHost': '',
        'larkBuyerBaseName': '',
        'larkBuyerTableName': '',
        'larkBuyerTargetVerified': False,
        # HubStudio purchase assistant reads one ordinary Sheet through the
        # same enterprise-app credential held in Keychain/DPAPI. These route
        # coordinates are local-only and never exposed through public config.
        'purchaseAssistantSpreadsheetToken': assistant_token,
        'purchaseAssistantSheetId': assistant_sheet_id,
        'purchaseAssistantCellRange': assistant_cell_range,
        'purchaseAssistantApiBase': 'https://open.feishu.cn/open-apis',
        'purchaseAssistantCacheTtlSeconds': 8,
        'purchaseAssistantSourceMode': 'team',
        'purchaseAssistantPersonalSpreadsheetToken': '',
        'purchaseAssistantPersonalSheetId': '',
        'purchaseAssistantPersonalCellRange': '',
        'purchaseAssistantPersonalSheetName': '',
        'purchaseAssistantTeamSpreadsheetToken': assistant_token,
        'purchaseAssistantTeamSheetId': assistant_sheet_id,
        'purchaseAssistantTeamCellRange': assistant_cell_range,
        'purchaseAssistantTeamSheetName': '',
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


def normalize_purchase_assistant_profiles(cfg):
    """Migrate the legacy single target into two non-public source profiles."""
    cfg = dict(cfg or {})
    mode = str(cfg.get('purchaseAssistantSourceMode') or '').strip().lower()
    if mode not in {'personal', 'team'}:
        mode = 'team'
    cfg['purchaseAssistantSourceMode'] = mode

    active_token = str(
        cfg.get('purchaseAssistantSpreadsheetToken') or '').strip()
    active_sheet = str(cfg.get('purchaseAssistantSheetId') or '').strip()
    active_range = str(
        cfg.get('purchaseAssistantCellRange') or 'A1:AQ').strip().upper()
    prefix = ('purchaseAssistantPersonal' if mode == 'personal'
              else 'purchaseAssistantTeam')
    if (active_token and active_sheet
            and not str(cfg.get(prefix + 'SpreadsheetToken') or '').strip()
            and not str(cfg.get(prefix + 'SheetId') or '').strip()):
        cfg[prefix + 'SpreadsheetToken'] = active_token
        cfg[prefix + 'SheetId'] = active_sheet
        cfg[prefix + 'CellRange'] = active_range

    for profile_mode, fallback_range in (
            ('Personal', 'A1:H'), ('Team', 'A1:AQ')):
        profile_prefix = 'purchaseAssistant' + profile_mode
        token = str(
            cfg.get(profile_prefix + 'SpreadsheetToken') or '').strip()
        sheet_id = str(cfg.get(profile_prefix + 'SheetId') or '').strip()
        cell_range = str(
            cfg.get(profile_prefix + 'CellRange') or '').strip().upper()
        if token and sheet_id and not cell_range:
            cfg[profile_prefix + 'CellRange'] = fallback_range

    selected_prefix = ('purchaseAssistantPersonal'
                       if mode == 'personal' else 'purchaseAssistantTeam')
    selected_token = str(
        cfg.get(selected_prefix + 'SpreadsheetToken') or '').strip()
    selected_sheet = str(
        cfg.get(selected_prefix + 'SheetId') or '').strip()
    selected_range = str(
        cfg.get(selected_prefix + 'CellRange') or '').strip().upper()
    if selected_token and selected_sheet and selected_range:
        cfg['purchaseAssistantSpreadsheetToken'] = selected_token
        cfg['purchaseAssistantSheetId'] = selected_sheet
        cfg['purchaseAssistantCellRange'] = selected_range
    else:
        cfg['purchaseAssistantSpreadsheetToken'] = ''
        cfg['purchaseAssistantSheetId'] = ''
        cfg['purchaseAssistantCellRange'] = selected_range or (
            'A1:H' if mode == 'personal' else 'A1:AQ')
    return cfg


def load_config():
    return local_config_service().load()


def save_config(cfg):
    return local_config_service().commit(
        cfg, source='compatibility_route')['config']


def masked_proxy_summary(cfg):
    custom = str((cfg or {}).get('proxyLink') or '').strip()
    if not custom:
        return '系统默认代理模板'
    try:
        parsed = urlparse(custom)
        hostname = str(parsed.hostname or '').strip()
        if not parsed.scheme or not hostname:
            return '自定义代理 · ••••'
        port = (':%d' % parsed.port) if parsed.port else ''
        auth = '••••@' if parsed.username or parsed.password else ''
        path = '/…' if parsed.path and parsed.path != '/' else ''
        return '%s://%s%s%s%s' % (
            parsed.scheme, auth, hostname, port, path)
    except (TypeError, ValueError):
        return '自定义代理 · ••••'


def public_config(cfg):
    result = {key: value for key, value in cfg.items()
              if key in CONFIG_FIELDS and key not in {
                  'proxyLink', 'larkBuyerBaseToken', 'larkBuyerTableId',
                  'larkBuyerTargetHost',
                  'larkBuyerBaseName', 'larkBuyerTableName',
                  'larkBuyerTargetVerified',
                  'purchaseAssistantSpreadsheetToken',
                  'purchaseAssistantSheetId',
                  'purchaseAssistantCellRange',
                  'purchaseAssistantApiBase',
                  'purchaseAssistantCacheTtlSeconds',
                  'purchaseAssistantSourceMode',
                  'purchaseAssistantPersonalSpreadsheetToken',
                  'purchaseAssistantPersonalSheetId',
                  'purchaseAssistantPersonalCellRange',
                  'purchaseAssistantPersonalSheetName',
                  'purchaseAssistantTeamSpreadsheetToken',
                  'purchaseAssistantTeamSheetId',
                  'purchaseAssistantTeamCellRange',
                  'purchaseAssistantTeamSheetName'}}
    result['proxyConfigured'] = bool(effective_proxy_link(cfg))
    result['proxySource'] = ('custom' if str(
        (cfg or {}).get('proxyLink') or '').strip() else 'default')
    result['proxyMasked'] = masked_proxy_summary(cfg)
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


def public_executor_config(cfg):
    """Return only device runtime and safety settings for cloud control."""
    public = public_config(cfg)
    return {
        key: public[key]
        for key in EXECUTOR_RUNTIME_CONFIG_FIELDS
        if key in public
    }


def local_config_service(path=None):
    """Return the single local-config authority for one resolved data path."""
    resolved = os.path.abspath(str(path or CONFIG_PATH))
    with _LOCAL_CONFIG_SERVICES_LOCK:
        service = _LOCAL_CONFIG_SERVICES.get(resolved)
        if service is None:
            service = LocalConfigService(
                resolved,
                allowed_fields=CONFIG_FIELDS,
                default_factory=default_config,
                normalizer=normalize_purchase_assistant_profiles,
                summary_projector=public_executor_config,
                audit_value_fields=(
                    set(EXECUTOR_RUNTIME_CONFIG_FIELDS) | {'serverPort'}),
            )
            _LOCAL_CONFIG_SERVICES[resolved] = service
        return service


def state_local_config_service():
    """Use AppState's service unless a compatibility test/path overrides it."""
    service = getattr(STATE, 'local_config', None)
    if (isinstance(service, LocalConfigService)
            and service.path == os.path.abspath(CONFIG_PATH)):
        return service
    return local_config_service()


def public_local_config(cfg, hub_api_key_store=None):
    """Expose local settings with an opaque full-config desktop revision."""
    result = public_config(cfg)
    result.update(public_hub_api_key_status(hub_api_key_store))
    result['configRevision'] = state_local_config_service().revision(cfg)
    return result


def public_envbatch_preferences(cfg):
    """Return only non-secret preferences needed by environment creation."""
    public = public_config(cfg)
    fields = (
        'purchaseSite', 'purchaseTags', 'importBuyerPlan',
        'verifySampleCount', 'buyers', 'buyerDefaultSplit', 'backupMaxCount',
    )
    return {key: public[key] for key in fields if key in public}


def updated_envbatch_preferences(old_cfg, body):
    """Apply the site/group choices exposed to environment operators."""
    if not isinstance(body, dict):
        raise ValueError('环境偏好请求必须是 JSON 对象')
    allowed = {'purchaseSite', 'purchaseTags'}
    unknown = set(body) - allowed
    if unknown:
        raise ValueError('环境偏好只允许修改站点和采购分组')
    if not body:
        raise ValueError('环境偏好没有可保存的字段')
    updated = updated_config(old_cfg, body)
    submitted_tags = body.get('purchaseTags')
    if isinstance(submitted_tags, dict):
        for site, value in submitted_tags.items():
            if str(value or '').strip():
                validate_purchase_group_site(value, site)
    return updated


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
    if 'safeParallelTasks' in body:
        if not isinstance(body.get('safeParallelTasks'), bool):
            raise ValueError('安全并行模式必须是布尔值')
        cfg['safeParallelTasks'] = body['safeParallelTasks']
    query_browser_mode = str(
        body.get('queryBrowserMode', cfg.get('queryBrowserMode') or 'headless')
    ).strip().casefold()
    if query_browser_mode not in ('headless', 'visible'):
        raise ValueError('物流查询浏览器模式必须是 headless 或 visible')
    cfg['queryBrowserMode'] = query_browser_mode
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


def updated_executor_config(old_cfg, body):
    """Apply cloud-managed device fields without touching business history."""
    if not isinstance(body, dict):
        raise ValueError('设备配置请求必须是 JSON 对象')
    unknown = set(body) - set(EXECUTOR_RUNTIME_CONFIG_FIELDS)
    if unknown:
        raise ValueError('设备配置包含不允许保存的字段')
    defaults = default_config()
    cfg = dict(defaults)
    cfg.update({key: value for key, value in (old_cfg or {}).items()
                if key in CONFIG_FIELDS})
    integer_ranges = {
        'hubPort': (1, 65535),
        'concurrency': (1, 5),
        'envCreateWorkers': (1, 10),
        'verifySampleCount': (0, 10),
    }
    for key, (minimum, maximum) in integer_ranges.items():
        raw = body[key] if key in body else cfg.get(key, defaults[key])
        try:
            if isinstance(raw, bool):
                raise ValueError
            value = int(raw)
        except (TypeError, ValueError):
            if key in body:
                raise ValueError('%s 必须是整数' % key) from None
            value = int(defaults[key])
        if not minimum <= value <= maximum:
            if key in body:
                raise ValueError('%s 超出允许范围' % key)
            value = int(defaults[key])
        cfg[key] = value
    parallel = body.get(
        'safeParallelTasks',
        cfg.get('safeParallelTasks', defaults['safeParallelTasks']))
    if not isinstance(parallel, bool):
        if 'safeParallelTasks' in body:
            raise ValueError('安全并行模式必须是布尔值')
        parallel = bool(defaults['safeParallelTasks'])
    cfg['safeParallelTasks'] = parallel
    query_browser_mode = str(body.get(
        'queryBrowserMode', cfg.get('queryBrowserMode') or 'headless'
    )).strip().casefold()
    if query_browser_mode not in ('headless', 'visible'):
        if 'queryBrowserMode' in body:
            raise ValueError('物流查询浏览器模式无效')
        query_browser_mode = 'headless'
    cfg['queryBrowserMode'] = query_browser_mode
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


def restore_lark_credentials(store, snapshot):
    """Restore a captured Keychain/DPAPI value without exposing it."""
    if snapshot is None:
        store.clear()
    else:
        store.save(snapshot.app_id, snapshot.app_secret)


def restore_hub_api_key(store, snapshot):
    if snapshot is None:
        store.clear()
    else:
        store.save(snapshot)


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
        self.capability = None
        self.checked_at = 0.0

    def reset(self):
        with self.lock:
            self.value = None
            self.error = ''
            self.capability = None
            self.checked_at = 0.0

    def snapshot(self, force=False):
        with self.lock:
            now = time.monotonic()
            if (not force and self.capability is not None
                    and now - self.checked_at < self.ttl_seconds):
                return dict(self.capability)
            hub = self.hub_getter()
            capability_getter = getattr(hub, 'capability_snapshot', None)
            if callable(capability_getter):
                capability = dict(capability_getter())
                ok = bool(capability.get('available'))
                err = '' if ok else str(capability.get('message') or '')
            else:
                ok, err = hub.ping_detail()
                capability = {
                    'available': bool(ok),
                    'clientRunning': bool(ok),
                    'localApiEnabled': bool(ok),
                    'authenticated': bool(ok),
                    'apiVersion': '',
                    'endpoint': '',
                    'reasonCode': 'ok' if ok else 'hubstudio_unavailable',
                    'message': '' if ok else str(err or ''),
                }
            # E010205 is a rate-limit response, not proof that the local
            # client disconnected. Preserve the last observed state.
            if (not ok and (
                    capability.get('reasonCode') ==
                    'hubstudio_local_api_rate_limited'
                    or 'E010205' in (err or ''))
                    and self.capability is not None):
                self.checked_at = now
                return dict(self.capability)
            self.value = bool(ok)
            self.error = err or ''
            self.capability = capability
            self.checked_at = now
            return dict(self.capability)

    def cached_snapshot(self):
        """Return the last probe without ever touching HubStudio Local API."""
        with self.lock:
            if self.capability is not None:
                return dict(self.capability)
            return {
                'available': False,
                'clientRunning': False,
                'localApiEnabled': False,
                'authenticated': False,
                'apiVersion': '',
                'endpoint': '',
                'reasonCode': 'hubstudio_check_pending',
                'message': 'HubStudio 状态正在后台检测',
            }

    def check(self, force=False):
        capability = self.snapshot(force=force)
        return (
            bool(capability.get('available')),
            '' if capability.get('available') else
            str(capability.get('message') or ''))


class HubReadCache(object):
    """Small read-through cache for slow, non-sensitive Hub list views."""

    def __init__(self):
        self.lock = threading.Lock()
        self.entries = {}

    def get(self, key, ttl_seconds, loader):
        with self.lock:
            now = time.monotonic()
            cached = self.entries.get(key)
            if cached and now - cached['storedAt'] < float(ttl_seconds):
                return copy.deepcopy(cached['value'])
            value = loader()
            self.entries[key] = {
                'storedAt': now,
                'value': copy.deepcopy(value),
            }
            return copy.deepcopy(value)

    def invalidate(self):
        with self.lock:
            self.entries = {}


class AppState(object):
    """进程级共享状态：配置 + HubStudio 连接 + 编排器。"""

    def __init__(self, credential_store=None, auth_service=None,
                 extension_bridge=None, hub_api_key_store=None,
                 executor_credential_store=None):
        self.local_config = local_config_service()
        self.config_lock = self.local_config.lock
        cfg = self.local_config.load()
        self.cfg = cfg
        self.data_sources = DataSourceRegistry(LOCAL_BINDINGS_PATH)
        self.data_source_registry_error = ''
        try:
            self.data_sources.migrate_legacy(cfg)
        except (DataSourceRegistryError, OSError, RuntimeError):
            # Keep the device channel and unrelated tools available. Any data
            # source resolution remains fail-closed until the file is repaired.
            self.data_source_registry_error = \
                'data_source_registry_migration_failed'
        self.auth = auth_service or LocalAuthService()
        self.executor_credential_store = (
            executor_credential_store or system_executor_credential_store())
        self.operation_sync_error = ''
        self.operation_sync = OperationResultSyncQueue(
            self._send_operation_result)
        self.extension_bridge = extension_bridge or ExtensionBridge()
        self.lark_credentials = credential_store or system_credential_store()
        self.hub_api_key_store = (
            hub_api_key_store or system_hub_api_key_store())
        self._config_summary_secure_lock = threading.RLock()
        self._config_summary_secure_cache = {}
        self.purchase_assistant = PurchaseAssistantService.from_runtime_config(
            cfg,
            transport_factory=lambda: CloudFeishuTransport(
                self.auth.feishu_read_request,
                'assistant.access',
                legacy_clearer=self.lark_credentials.clear,
            ))
        # 团队 Local API 配额已于 2026-09-04 提升到 300 次/分钟。
        # 仍保留 0.3 秒全局错峰（理论上限约 200 次/分钟），并把同时在途
        # 请求压到 3。现场故障是 HubStudio 本地进程/浏览器资源压力，而非
        # 云端额度；用满 300 次额度反而会放大本地监听器抖动。
        self.hub_runtime_gate = HubRuntimeGate(
            max_requests=3, min_request_interval=0.3)
        self.tasks = LocalTaskCoordinator(
            lambda: bool(self.cfg.get('safeParallelTasks')))
        self.hub = self._build_hub_adapter()
        self.hub_core_repair = HubCoreRepairCoordinator(
            lambda: self.hub, self.tasks, HUB_CORE_AUDIT_PATH,
            device_info_getter=lambda: {
                **ExecutorChannelStateStore().load(),
                'clientVersion': __version__,
            })
        self._hub_status = HubStatusCache(lambda: self.hub)
        self._hub_reads = HubReadCache()
        self.orch = QueryOrchestrator(
            self.hub, log_dir=LOG_DIR,
            concurrency=cfg.get('concurrency', 2))
        self.reg_job = RegistrationJob(lambda: self.hub)
        self.buyer_library = BuyerLibraryJob(
            lambda: DatabaseBuyerLibraryService(
                lambda path, method='GET', payload=None:
                    self.auth.buyer_account_request(
                        path, method=method, payload=payload,
                        permission=(
                            'resource.buyer.read'
                            if method == 'GET' else
                            'resource.buyer.import'))))
        self.env_job = EnvBatchJob(
            lambda: self.hub, lambda: self.cfg,
            group_getter=self.hub_groups)
        self.backup_job = BackupEnvJob(lambda: self.hub, lambda: self.cfg)
        self.resources = ResourceCenterService(
            lambda: self.hub,
            lambda: None,
            transport_factory=lambda permission: CloudFeishuTransport(
                self.auth.feishu_read_request,
                permission,
                legacy_clearer=self.lark_credentials.clear,
            ))
        self.procurement_import = ProcurementImportService()
        install_mode = str(
            os.environ.get('XYNIGO_INSTALL_MODE') or 'green'
        ).strip().casefold()
        update_client = (
            StandardInstallerUpdateClient(self.auth)
            if (install_mode == 'standard'
                and (os.name == 'nt' or sys.platform == 'darwin')) else None)
        self.updates = UpdateCoordinator(
            os.environ.get('XYNIGO_INSTALL_DIR'),
            __version__,
            client=update_client,
            current_runtime_id=os.environ.get('XYNIGO_RUNTIME_ID'),
        )
        self.executor_channel = ExecutorChannelWorker(
            client=CloudExecutorClient(),
            credential_store=self.executor_credential_store,
            state_store=ExecutorChannelStateStore(),
            config_getter=lambda: dict(self.cfg),
            public_config_getter=public_executor_config,
            config_writer=self.apply_cloud_config,
            task_coordinator=self.tasks,
            hub_status_getter=self.hub_capabilities,
            config_summary_getter=self.config_summary_v2,
            # Device identity keeps the background channel alive.  A desktop
            # user session must come from interactive Feishu OAuth and must
            # never be reinstalled automatically after logout/switch-user.
            user_session_installer=None,
        )

    def _send_operation_result(self, endpoint, payload, permission):
        """Attach native device proof without persisting it in the outbox."""
        executor_credential = None
        if endpoint == '/v1/operations/logistics-query-runs':
            executor_credential = self.executor_credential_store.load()
        return self.auth.operation_result_request(
            endpoint, payload, permission,
            executor_credential=executor_credential)

    def config_summary_v2(self):
        """Return the strict, non-sensitive device summary sent to cloud."""
        base = self.local_config.summary(self.cfg)
        issue_codes = []
        try:
            registry = self.data_sources.snapshot()['registry']
        except Exception:
            registry = {
                'dataSources': [],
                'buyerProfiles': [],
                'environmentBindings': [],
                'teamDefaultDataSourceId': '',
            }
            issue_codes.append('data_source_registry_unavailable')
        if self.data_source_registry_error:
            if 'data_source_registry_unavailable' not in issue_codes:
                issue_codes.append('data_source_registry_unavailable')
        sources = list(registry.get('dataSources') or [])
        pending_owners = sum(
            1 for item in sources
            if item.get('scope') == 'personal'
            and item.get('migrationState') == 'needs_owner_confirmation')
        secure_status = AppState._config_summary_secure_status(self)
        lark_configured = secure_status['larkAppCredentials']
        hub_key_configured = secure_status['hubApiKey']
        issue_codes.extend(secure_status['issueCodes'])
        legacy_target_configured = bool(
            str(self.cfg.get('larkBuyerBaseToken') or '').strip()
            and str(self.cfg.get('larkBuyerTableId') or '').strip())
        return {
            'schemaVersion': 2,
            'configRevision': base['configRevision'],
            'capturedAt': datetime.now(timezone.utc).isoformat(),
            'runtimeConfig': base['runtimeConfig'],
            'configured': {
                'hubApiKey': hub_key_configured,
                'larkAppCredentials': lark_configured,
                'larkLegacyTarget': legacy_target_configured,
                'purchaseAssistantDataSources': bool(sources),
                'teamDefaultDataSource': bool(
                    registry.get('teamDefaultDataSourceId')),
            },
            'dataSources': {
                'dataSourceCount': len(sources),
                'buyerProfileCount': len(
                    registry.get('buyerProfiles') or []),
                'environmentBindingCount': len(
                    registry.get('environmentBindings') or []),
                'pendingOwnerConfirmationCount': pending_owners,
                'mappingConflictCount': 0,
            },
            'compliance': {
                'status': 'ready' if not issue_codes else 'degraded',
                'issueCodes': sorted(set(issue_codes)),
            },
        }

    def _config_summary_secure_status(self):
        """Cache Keychain/DPAPI presence checks to avoid polling the OS."""
        lock = getattr(self, '_config_summary_secure_lock', None)
        if lock is None:
            lock = threading.RLock()
            self._config_summary_secure_lock = lock
        with lock:
            cache = getattr(self, '_config_summary_secure_cache', {})
            checked_at = float(cache.get('checkedAt') or 0)
            if cache and time.monotonic() - checked_at < 300:
                return copy.deepcopy(cache['status'])
            issues = []
            try:
                lark_configured = self.lark_credentials.load() is not None
            except Exception:
                lark_configured = False
                issues.append('lark_credential_store_unavailable')
            try:
                hub_configured = self.hub_api_key_store.load() is not None
            except Exception:
                hub_configured = False
                issues.append('hub_api_key_store_unavailable')
            status = {
                'larkAppCredentials': lark_configured,
                'hubApiKey': hub_configured,
                'issueCodes': issues,
            }
            self._config_summary_secure_cache = {
                'checkedAt': time.monotonic(),
                'status': copy.deepcopy(status),
            }
            return status

    def invalidate_config_summary_secure_status(self):
        lock = getattr(self, '_config_summary_secure_lock', None)
        if lock is None:
            self._config_summary_secure_cache = {}
            return
        with lock:
            self._config_summary_secure_cache = {}

    def apply_cloud_config(self, submitted):
        """Validate and atomically apply a non-secret cloud config task."""
        with self.config_lock:
            old_cfg = dict(self.cfg)
            cfg = updated_executor_config(old_cfg, submitted)
            committed = self.local_config.commit(
                cfg, source='cloud_legacy_config_write')
            cfg = committed['config']
            self.cfg = cfg
            reconnect_needed = (
                cfg.get('hubPort') != old_cfg.get('hubPort')
                or cfg.get('concurrency') != old_cfg.get('concurrency'))
            if reconnect_needed:
                try:
                    self.reconnect_hub()
                except Exception:
                    # The config write itself is durable. The next heartbeat
                    # reports Hub offline without rolling the file back.
                    pass
            return dict(cfg)

    def purchase_assistant_for_member(self, member_id, container_code=''):
        """Resolve one member/environment source into an isolated provider."""
        if self.data_source_registry_error:
            raise DataSourceMappingRequired()
        source, resolution = self.data_sources.resolve_with_context(
            member_id,
            container_code=container_code,
            allow_team_default=True,
        )
        runtime_config = runtime_config_for_source(self.cfg, source)
        service = self.purchase_assistant.for_runtime_config(runtime_config)
        status = service.source_status()
        status.update({
            'dataSourceId': source['id'],
            'scope': source['scope'],
            'label': source['label'],
            'resolution': resolution,
        })
        status['active'].update({
            'dataSourceId': source['id'],
            'scope': source['scope'],
            'label': source['label'],
            'resolution': resolution,
        })
        return service, status

    def apply_purchase_assistant_source(self, member_id, mode,
                                        validation_id='',
                                        expected_revision=None):
        """Apply an extension source choice to the signed-in member only."""
        selected_mode = str(mode or '').strip().lower()
        if selected_mode not in {'personal', 'team'}:
            raise PurchaseAssistantError('收件信息数据源类型无效')
        if selected_mode == 'personal':
            if str(validation_id or '').strip():
                target = self.purchase_assistant.consume_validated_target(
                    validation_id, owner_key=member_id)
                self.data_sources.upsert_personal(
                    member_id, target,
                    expected_revision=expected_revision)
            else:
                self.data_sources.resolve(
                    member_id, allow_team_default=False)
        else:
            self.data_sources.use_team_default(
                member_id, expected_revision=expected_revision)
        _service, status = self.purchase_assistant_for_member(member_id)
        return status

    def _build_hub_adapter(self):
        self.hub_api_key_error = ''
        try:
            api_key = self.hub_api_key_store.load()
        except HubApiKeyStoreError:
            api_key = None
            self.hub_api_key_error = 'hubstudio_local_api_key_unavailable'
        return HubStudioApi(
            port=self.cfg['hubPort'], api_key=api_key,
            runtime_gate=self.hub_runtime_gate)

    def save_hub_api_key(self, value=None, clear=False):
        transaction = SecureStoreTransaction(
            self.hub_api_key_store.load,
            lambda snapshot: restore_hub_api_key(
                self.hub_api_key_store, snapshot),
            'HubStudio API Key',
        )
        with transaction:
            transaction.mutate(
                self.hub_api_key_store.clear if clear else
                lambda: self.hub_api_key_store.save(value))
            try:
                self.reconnect_hub()
            except Exception:
                # Restore both the secure value and the in-memory adapter.  A
                # second reconnect failure must not hide the original failure.
                transaction.rollback()
                try:
                    self.reconnect_hub()
                except Exception:
                    pass
                raise
            transaction.commit()
        AppState.invalidate_config_summary_secure_status(self)
        return {
            'saved': True,
            'configured': not bool(clear),
            'masked': public_hub_api_key_status(
                self.hub_api_key_store)['hubApiKeyMasked'],
            'capability': self.hub_capabilities(force=True),
        }

    def reconnect_hub(self):
        self.orch.close()
        self.hub = self._build_hub_adapter()
        self.orch = QueryOrchestrator(
            self.hub, log_dir=LOG_DIR,
            concurrency=self.cfg.get('concurrency', 2))
        self._hub_status.reset()
        self._hub_reads.invalidate()
        self.resources.invalidate()
        return self.hub_status(force=True)[0]

    def hub_status(self, force=False):
        return self._hub_status.check(force=force)

    def hub_capabilities(self, force=False):
        return self._hub_status.snapshot(force=force)

    def hub_groups(self):
        return self._hub_reads.get(
            ('groups',), 30.0, lambda: self.hub.group_list())

    def hub_group_serials(self, group=None):
        normalized_group = str(group or '')

        def load():
            envs = self.hub.env_list(normalized_group or None)
            return sorted(
                [str(item.get('serialNumber')) for item in envs
                 if item.get('serialNumber') is not None],
                key=lambda value: int(value) if value.isdigit() else 0)

        return self._hub_reads.get(
            ('group-envs', normalized_group), 5.0, load)

    def workspace_snapshot(self):
        """Build one credential-free snapshot for cloud workspace restore."""
        preferences = public_envbatch_preferences(self.cfg)
        runtime_config = public_executor_config(self.cfg)
        runtime_config = {
            'configRevision': config_revision(runtime_config),
            **runtime_config,
        }
        groups = sorted({
            str(item or '').strip() for item in self.hub_groups()
            if str(item or '').strip()
        })
        try:
            configured_workers = max(
                1, min(10, int(self.cfg.get('envCreateWorkers') or 5)))
        except (TypeError, ValueError):
            configured_workers = 5
        preflight = {}
        for site in ('MX', 'US'):
            try:
                source = self.env_job.preflight(site)
                preflight[site] = {
                    'ready': bool(source.get('ready')),
                    'hubConnected': bool(source.get('hubConnected')),
                    'groupFound': bool(source.get('groupFound')),
                    'proxyConfigured': bool(source.get('proxyConfigured')),
                    'purchaseTag': str(source.get('purchaseTag') or ''),
                    'configuredWorkers': int(
                        source.get('configuredWorkers') or configured_workers),
                    'effectiveWorkers': int(
                        source.get('effectiveWorkers') or configured_workers),
                    'message': scrub_text(source.get('message') or '')[:300],
                }
            except Exception as exc:
                preflight[site] = {
                    'ready': False,
                    'hubConnected': bool(self.hub_status()[0]),
                    'groupFound': False,
                    'proxyConfigured': bool(effective_proxy_link(self.cfg)),
                    'purchaseTag': str(
                        purchase_tag_for_site(self.cfg, site) or '')[:12],
                    'configuredWorkers': configured_workers,
                    'effectiveWorkers': configured_workers,
                    'message': scrub_text(exc)[:300],
                }
        captured_at = datetime.now(timezone.utc).isoformat()
        content = {
            'preferences': preferences,
            'runtimeConfig': runtime_config,
            'groups': groups,
            'preflight': preflight,
        }
        return {
            'schemaVersion': 1,
            'snapshotRevision': config_revision(content),
            'capturedAt': captured_at,
            **content,
        }

    def local_executor_status(self):
        """Return the non-sensitive status contract used by the Windows tray.

        This endpoint is intentionally independent from the cloud user session so
        the local status center remains useful while the user is signed out.  It
        never returns device credentials, cloud sessions, config values, task
        identifiers, HubStudio response bodies or user data.
        """
        channel = ExecutorChannelStateStore().load()
        tasks = self.tasks.snapshot()
        if hasattr(self, '_hub_status'):
            hub_capability = self._hub_status.cached_snapshot()
        else:
            # Compatibility for an upgrading status-center process or a
            # minimal state fixture that only exposes the legacy tuple API.
            legacy_ok, _legacy_error = self.hub_status(force=False)
            hub_capability = {
                'available': bool(legacy_ok),
                'clientRunning': bool(legacy_ok),
                'localApiEnabled': bool(legacy_ok),
                'authenticated': bool(legacy_ok),
                'apiVersion': '',
                'endpoint': '',
                'reasonCode': ('ok' if legacy_ok else
                               'hubstudio_unavailable'),
                'message': ('' if legacy_ok else
                            'HubStudio 自动化暂不可用'),
            }
        hub_ok = bool(hub_capability.get('available'))
        raw_core_repair = (
            self.hub_core_repair.snapshot()
            if hasattr(self, 'hub_core_repair') else {
                'state': 'idle', 'running': False,
                'browserType': '', 'coreVersion': '',
                'message': '', 'errorCode': '',
                'repairAvailable': False,
            })
        core_repair = {
            'state': str(raw_core_repair.get('state') or 'idle'),
            'running': bool(raw_core_repair.get('running')),
            'browserType': str(
                raw_core_repair.get('browserType') or '')[:16],
            'coreVersion': str(
                raw_core_repair.get('coreVersion') or '')[:8],
            'message': scrub_text(
                raw_core_repair.get('message') or '')[:240],
            'errorCode': str(
                raw_core_repair.get('errorCode') or '')[:80],
            'repairAvailable': bool(
                raw_core_repair.get('repairAvailable')),
            'auditState': str(
                raw_core_repair.get('auditState') or '')[:32],
            'startedAt': raw_core_repair.get('startedAt'),
            'finishedAt': raw_core_repair.get('finishedAt'),
        }
        required_core = hub_capability.get('requiredCore')
        required_core = required_core if isinstance(required_core, dict) \
            else {}
        channel_status = str(channel.get('status') or 'not_paired')
        paired = bool(channel.get('executorId')) and channel_status not in {
            'not_paired', 'revoked', 'credential_error'}
        safe_tasks = [{
            'kind': str(item.get('kind') or ''),
            'label': str(item.get('label') or '后台任务'),
            'state': 'running',
            'startedAt': str(item.get('startedAt') or ''),
            'elapsedSec': int(item.get('elapsedSec') or 0),
            'resourceCount': max(
                0, int(item.get('resourceCount') or 0)),
        } for item in (tasks.get('tasks') or [])]
        update = self.updates.snapshot()
        return {
            'schemaVersion': 1,
            'product': 'Xynigo Sourcing 本地执行器',
            'version': __version__,
            'executor': {
                'running': True,
                'paired': paired,
                'displayName': str(channel.get('displayName') or ''),
                'platform': str(channel.get('platform') or ''),
                'architecture': str(channel.get('architecture') or ''),
            },
            'cloudChannel': {
                'status': channel_status,
                'lastPollAt': channel.get('lastPollAt'),
                'lastErrorCode': str(channel.get('lastErrorCode') or ''),
                'phase': str(channel.get('connectionPhase') or ''),
                'attempt': int(channel.get('connectionAttempt') or 0),
                'nextRetryAt': channel.get('nextRetryAt'),
                'connectedAt': channel.get('connectedAt'),
            },
            'hubStudio': {
                'connected': bool(hub_ok),
                'status': 'ready' if hub_ok else 'offline',
                'available': bool(hub_capability.get('available')),
                'clientRunning': bool(
                    hub_capability.get('clientRunning')),
                'localApiEnabled': bool(
                    hub_capability.get('localApiEnabled')),
                'authenticated': bool(
                    hub_capability.get('authenticated')),
                'apiVersion': str(
                    hub_capability.get('apiVersion') or ''),
                'endpoint': str(hub_capability.get('endpoint') or ''),
                'reasonCode': str(
                    hub_capability.get('reasonCode') or ''),
                'message': str(hub_capability.get('message') or ''),
                'requiredCore': ({
                    'browserType': str(
                        required_core.get('browserType') or '')[:16],
                    'version': str(
                        required_core.get('version') or '')[:8],
                } if required_core else {}),
                'coreRepair': core_repair,
            },
            'tasks': {
                'activeCount': len(safe_tasks),
                'safeParallel': bool(tasks.get('safeParallel')),
                'items': safe_tasks,
            },
            'update': {
                'enabled': bool(update.get('enabled')),
                'state': str(update.get('state') or 'disabled'),
                'stage': str(update.get('stage') or ''),
                'installMode': str(update.get('installMode') or ''),
                'installFlow': str(update.get('installFlow') or ''),
                'currentVersion': str(update.get('currentVersion') or ''),
                'currentRuntimeId': str(
                    update.get('currentRuntimeId') or ''),
                'latestVersion': str(update.get('latestVersion') or ''),
                'latestRuntimeId': str(
                    update.get('latestRuntimeId') or ''),
                'message': str(update.get('message') or ''),
                'downloadReceivedBytes': int(
                    update.get('downloadReceivedBytes') or 0),
                'downloadTotalBytes': int(
                    update.get('downloadTotalBytes') or 0),
                'downloadPercent': max(
                    0, min(100, int(update.get('downloadPercent') or 0))),
                'downloadSpeedBytesPerSecond': int(
                    update.get('downloadSpeedBytesPerSecond') or 0),
                'downloadEtaSeconds': (
                    int(update['downloadEtaSeconds'])
                    if update.get('downloadEtaSeconds') is not None else None),
            },
        }

    @staticmethod
    def _iso_epoch(value):
        if not value:
            return None
        return datetime.fromtimestamp(float(value), timezone.utc).isoformat()

    @staticmethod
    def _iso_local_text(value, utc_offset_minutes=None):
        text = str(value or '').strip()
        if not text:
            return None
        try:
            parsed = datetime.strptime(text, '%Y-%m-%d %H:%M:%S')
            if utc_offset_minutes is None:
                return parsed.astimezone().isoformat()
            zone = timezone(timedelta(minutes=int(utc_offset_minutes)))
            return parsed.replace(tzinfo=zone).isoformat()
        except (TypeError, ValueError, OverflowError):
            return None

    def environment_result_payload(self, task_id, account_ids=None):
        snapshot = self.env_job.snapshot()
        runner = self.env_job.runner
        if runner is None:
            raise ValueError('建环境任务没有可回传结果')
        selected = set(account_ids or ())
        rows = [row for row in snapshot.get('rows') or []
                if not selected or str(row.get('accountId') or '') in selected]
        if not rows:
            raise ValueError('建环境任务没有可回传行')
        return {
            'source': 'local_executor',
            'runKey': task_id,
            'site': runner.site,
            'purchaseDate': runner.purchase_date,
            'environmentGroup': runner.purchase_tag,
            'startedAt': self._iso_epoch(self.env_job.started_at),
            'completedAt': self._iso_epoch(
                self.env_job.finished_at or time.time()),
            'results': [{
                'accountRef': str(row.get('accountId') or ''),
                'accountLabel': str(row.get('emailMasked') or ''),
                'purchaserLabel': str(row.get('buyer') or ''),
                'environmentName': str(row.get('envName') or ''),
                'environmentRef': (
                    str(row.get('containerCode'))
                    if row.get('containerCode') not in (None, '') else None),
                'environmentSerial': (
                    str(row.get('serialNumber'))
                    if row.get('serialNumber') not in (None, '') else None),
                'status': (
                    'success' if row.get('state') == 'done'
                    else 'stopped' if row.get('state') in (
                        'stopped', 'rolled_back')
                    else 'failed'),
                'errorStep': str(row.get('errorStep') or ''),
                'errorSummary': scrub_text(row.get('error') or '')[:300],
                'bindingAt': self._iso_local_text(row.get('bindingTime')),
                'recoveredExisting': bool(row.get('recoveredExisting')),
                'createdInRun': bool(row.get('createdInRun')),
                'cleanupStatus': str(
                    row.get('cleanupStatus') or 'not_required'),
                'cleanupErrorCode': str(
                    row.get('cleanupErrorCode') or ''),
                'cleanupErrorSummary': scrub_text(
                    row.get('cleanupError') or '')[:300],
            } for row in rows],
            'ipChecks': [{
                'environmentName': str(item.get('envName') or ''),
                'ipAddress': str(item.get('ip') or ''),
                'country': str(item.get('country') or ''),
                'city': str(item.get('city') or ''),
                'isp': str(item.get('isp') or ''),
                'ok': bool(item.get('ok')),
                'errorCode': str(item.get('errorCode') or ''),
                'errorSummary': scrub_text(item.get('error') or '')[:300],
            } for item in snapshot.get('ipChecks') or []
                if (not selected or any(
                    str(row.get('envName') or '') ==
                    str(item.get('envName') or '') for row in rows))],
        }

    def backup_environment_result_payload(
            self, run_key, site, purchase_date, environment_group,
            purchaser_label):
        snapshot = self.backup_job.snapshot()
        rows = [dict(row) for row in snapshot.get('rows') or []]
        if not rows:
            raise ValueError('备用/测试建环境任务没有可回传行')
        ip_by_name = {
            str(item.get('envName') or ''): dict(item)
            for item in snapshot.get('ipChecks') or []
        }
        return {
            'source': 'local_executor',
            'runKey': run_key,
            'site': normalize_env_site(site),
            'purchaseDate': str(purchase_date or ''),
            'environmentGroup': validate_purchase_group_site(
                environment_group, site),
            'startedAt': self._iso_epoch(self.backup_job.started_at),
            'completedAt': self._iso_epoch(
                self.backup_job.finished_at or time.time()),
            'results': [{
                'accountRef': backup_account_ref(
                    run_key, str(row.get('envName') or '')),
                'accountLabel': '备用环境-%03d' % (index + 1),
                'purchaserLabel': str(purchaser_label or ''),
                'environmentName': str(row.get('envName') or ''),
                'environmentRef': (
                    str(row.get('containerCode'))
                    if row.get('containerCode') not in (None, '') else None),
                'environmentSerial': (
                    str(row.get('serialNumber'))
                    if row.get('serialNumber') not in (None, '') else None),
                'status': (
                    'success' if row.get('state') == 'done'
                    else 'stopped' if row.get('state') in (
                        'stopped', 'rolled_back')
                    else 'failed'),
                'errorStep': (
                    '' if row.get('state') == 'done'
                    else 'environment_create'),
                'errorSummary': scrub_text(row.get('error') or '')[:300],
                'bindingAt': None,
                'recoveredExisting': False,
                'createdInRun': bool(row.get('createdInRun')),
                'cleanupStatus': str(
                    row.get('cleanupStatus') or 'not_required'),
                'cleanupErrorCode': str(
                    row.get('cleanupErrorCode') or ''),
                'cleanupErrorSummary': scrub_text(
                    row.get('cleanupError') or '')[:300],
            } for index, row in enumerate(rows)],
            'ipChecks': [{
                'environmentName': environment_name,
                'ipAddress': str(item.get('ip') or ''),
                'country': str(item.get('country') or ''),
                'city': str(item.get('city') or ''),
                'isp': str(item.get('isp') or ''),
                'ok': bool(item.get('ok')),
                'errorCode': str(item.get('errorCode') or ''),
                'errorSummary': scrub_text(item.get('error') or '')[:300],
            } for environment_name, item in ip_by_name.items()],
        }

    def logistics_result_payload(self, task_id, mode, serials):
        snapshot = self.orch.snapshot()
        selected = {str(serial) for serial in serials or ()}
        rows = [row for row in snapshot.get('rows') or []
                if str(row.get('serial') or '') in selected]
        if not rows:
            raise ValueError('物流查询任务没有可回传行')
        allowed_states = {'ok', 'fail', 'login', 'inuse', 'stopped', 'pending'}
        return {
            'source': 'local_executor',
            'runKey': task_id,
            'queryMode': mode,
            'site': snapshot.get('site') or rows[0].get('site') or 'MX',
            'startedAt': self._iso_epoch(self.orch.started_at),
            'completedAt': self._iso_epoch(
                self.orch.finished_at or time.time()),
            'results': [{
                'environmentSerial': str(row.get('serial') or ''),
                'environmentName': str(row.get('envName') or ''),
                'status': (row.get('state') if row.get('state') in allowed_states
                           else 'fail'),
                'platformOrderNo': str(row.get('orderNo') or ''),
                'orderTime': str(row.get('orderTime') or ''),
                'amount': str(row.get('amount') or ''),
                'platformStatus': str(row.get('status') or ''),
                'statusLabel': str(row.get('statusCn') or ''),
                'fulfillmentStage': str(row.get('stage') or ''),
                'trackingNumbers': [str(item) for item in row.get('tracks') or []],
                'packageNumbers': [str(item) for item in row.get('pkgs') or []],
                'carrier': str(row.get('carrier') or ''),
                'firstTrackingAt': str(row.get('firstTrackingAt') or '') or None,
                'firstTrackingTime': str(row.get('firstTrackingTime') or ''),
                'firstTrackingSummary': str(
                    row.get('firstTrackingSummary') or '')[:300],
                'firstTrackingLeadMinutes': row.get(
                    'firstTrackingLeadMinutes'),
                'cancelled': bool(row.get('kanDan')),
                'riskOrder': bool(row.get('riskOrder')),
                'riskSummary': str(row.get('riskMessage') or ''),
                'ipAddress': str(row.get('ip') or ''),
                'timeZone': str(row.get('timeZone') or ''),
                'utcOffsetMinutes': row.get('utcOffsetMinutes'),
                'queriedAt': self._iso_local_text(
                    row.get('time'), row.get('utcOffsetMinutes')),
                'errorSummary': scrub_text(row.get('error') or '')[:300],
                'screenshotStatus': str(row.get('screenshotState') or ''),
            } for row in rows],
        }

    def enqueue_operation_result(self, endpoint, permission, payload):
        try:
            self.operation_sync.enqueue(endpoint, permission, payload)
            self.operation_sync_error = ''
        except Exception as exc:
            self.operation_sync_error = scrub_text(exc)[:200]
            raise


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
              write_lark_ledger=False, confirm_lark_write=False,
              on_finished=None):
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
            try:
                runner = RegistrationOrchestrator(
                    self.hub_getter(), accept_terms=True,
                    acknowledge_ms_privacy=acknowledge_ms_privacy,
                    ledger_sink=ledger_sink,
                    close_on_success=not keep_open)
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
                if on_finished:
                    try:
                        on_finished()
                    except Exception:
                        pass

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


def environment_worker_policy(cfg):
    """Return configured/effective HubStudio environment write workers.

    HubStudio's account-wide request quota is not its local process capacity.
    Environment creation performs several sequential writes per row and can
    run next to a logistics query, so keep enough Local API capacity for
    browser cleanup and health probes.  The configured value remains visible
    for audit while the effective value is the reliability boundary.
    """
    cfg = dict(cfg or {})
    try:
        configured = max(
            1, min(10, int(cfg.get('envCreateWorkers') or 5)))
    except (TypeError, ValueError):
        configured = 5
    cap = 2 if bool(cfg.get('safeParallelTasks')) else 3
    return configured, min(configured, cap)


class EnvBatchJob(object):
    """模块三后台任务：凭证仅保存在短生命周期内存对象。"""

    MAX_UPLOAD_BYTES = 20 * 1024 * 1024
    PENDING_TTL_SECONDS = 30 * 60
    RESULT_CREDENTIAL_TTL_SECONDS = 15 * 60

    def __init__(self, hub_getter, config_getter=load_config,
                 ledger_sync_factory=None, group_getter=None):
        self.hub_getter = hub_getter
        self.config_getter = config_getter
        self.ledger_sync_factory = ledger_sync_factory
        self.group_getter = group_getter
        self.lock = threading.Lock()
        self.pending = {}
        self.running = False
        self.stop_requested = False
        self.stop_available = False
        self.stop_event = threading.Event()
        self.started_at = None
        self.finished_at = None
        self.rows = []
        self.runner = None
        self.summary = {}
        self.ip_checks = []
        self.phase = 'idle'
        self.ip_check_total = 0
        self.fatal_error = ''
        self.fatal_error_code = ''
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

    def _runtime_config(self, site='MX', environment_group=None):
        site = normalize_env_site(site)
        cfg = dict(self.config_getter() or {})
        configured_workers, workers = environment_worker_policy(cfg)
        return {
            'site': site,
            'purchaseTag': validate_purchase_group_site(
                environment_group
                if environment_group is not None
                else purchase_tag_for_site(cfg, site),
                site),
            'proxyLink': effective_proxy_link(cfg),
            'workers': workers,
            'configuredWorkers': configured_workers,
        }

    def preflight(self, site='MX', environment_group=None):
        runtime = self._runtime_config(site, environment_group)
        result = envbatch_preflight(
            self.hub_getter(), runtime['purchaseTag'], runtime['proxyLink'],
            site=runtime['site'],
            groups=(self.group_getter() if self.group_getter else None))
        result.update({
            'configuredWorkers': runtime['configuredWorkers'],
            'effectiveWorkers': runtime['workers'],
        })
        return result

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

    def parse(self, filename, content_base64, site='MX'):
        source = self._decode_xlsx(content_base64)
        accounts = parse_vendor_workbook(BytesIO(source))
        site = normalize_env_site(site)
        # 环境创建临时兼容号商提供的 MX/US 混合登录态，Cookie 原文
        # 不裁剪；仅 Cookie 完全属于另一站时在保存计划前整批拒收。
        validate_accounts_site(accounts, site, allow_mixed=True)
        return self._store_pending_plan(filename, source, accounts, site)

    def import_cloud_plan(self, accounts, site='MX', filename='云端解析计划.xlsx',
                          cloud_plan_id=''):
        """Hydrate an authenticated cloud plan into short-lived local memory."""
        site = normalize_env_site(site)
        parsed = deserialize_buyer_accounts(accounts, site=site)
        cloud_plan_id = str(cloud_plan_id or '').strip()
        if not cloud_plan_id or len(cloud_plan_id) > 128:
            raise ValueError('云端解析计划编号无效')
        result = self._store_pending_plan(filename, b'', parsed, site)
        with self.lock:
            pending = self.pending.get(result['planId'])
            if pending is not None:
                pending['cloudPlanId'] = cloud_plan_id
        return {'cloudPlanId': cloud_plan_id, **result}

    def _store_pending_plan(self, filename, source, accounts, site):
        mixed_site_cookie_count = count_mixed_site_accounts(accounts)
        token = secrets.token_urlsafe(24)
        with self.lock:
            self._clean_pending()
            self.pending[token] = {
                'filename': os.path.basename(str(filename or '号商名单.xlsx')),
                'source': source,
                'accounts': accounts,
                'site': site,
                'createdAt': time.time(),
            }
        timer = threading.Timer(
            self.PENDING_TTL_SECONDS, self._expire_pending, args=(token,))
        timer.daemon = True
        timer.start()
        return {
            'planId': token,
            'site': site,
            'count': len(accounts),
            'cookieCount': sum(bool(item.cookie_text) for item in accounts),
            'mixedSiteCookieCount': mixed_site_cookie_count,
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

    def preview(self, plan_id, assignment, purchase_date, site='MX',
                environment_group=None, include_inventory_snapshot=False):
        with self.lock:
            self._clean_pending()
            pending = self.pending.get(plan_id)
        if not pending:
            raise ValueError('解析计划已过期，请重新选择 xlsx')
        parse_assignment(assignment, len(pending['accounts']))
        runtime = self._runtime_config(site, environment_group)
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
        rows = [{
            'emailMasked': row.account.safe_email,
            'buyer': row.account.buyer,
            'envName': row.env_name,
            'recoveredExisting': row.recovered_existing,
        } for row in plan]
        if include_inventory_snapshot:
            return rows, build_environment_inventory_snapshot(all_existing)
        return rows

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

    def _set_ip_checks(self, checks):
        with self.lock:
            self.ip_checks = [dict(item) for item in checks]

    @staticmethod
    def _fatal_reason(exc):
        if isinstance(exc, HubApiError):
            return exc.reason_code or 'hubstudio_local_api_error'
        if isinstance(exc, TaskConflict):
            return 'environment_resource_conflict'
        if isinstance(exc, EnvBatchError):
            return 'environment_preflight_failed'
        if isinstance(exc, ValueError):
            return 'environment_request_rejected'
        return 'environment_task_failed'

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
              environment_group=None,
              write_lark_ledger=False, confirm_lark_write=False,
              reserve_resources=None, on_finished=None,
              cleanup_blocked_account_refs=None, defer_preflight=False,
              planned_environment_names=None, trust_cloud_inventory=False):
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
        if planned_environment_names is not None:
            if not isinstance(planned_environment_names, list):
                raise ValueError('云端预占环境名格式无效')
            planned_name_map = {}
            for item in planned_environment_names:
                if not isinstance(item, dict) or set(item) != {
                        'accountRef', 'environmentName'}:
                    raise ValueError('云端预占环境名格式无效')
                account_ref = str(item.get('accountRef') or '').strip()
                environment_name = str(
                    item.get('environmentName') or '').strip()
                if not account_ref or not environment_name:
                    raise ValueError('云端预占环境名格式无效')
                planned_name_map[account_ref] = environment_name
            if len(planned_name_map) != account_count:
                raise ValueError('云端预占环境名数量无效')
        else:
            planned_name_map = None
        runtime = self._runtime_config(site, environment_group)
        # 正式执行同步复核：允许混合登录态，纯错站仍在消费计划前拒收。
        validate_accounts_site(
            pending['accounts'], runtime['site'], allow_mixed=True)
        hub = self.hub_getter()
        selected_existing = None
        all_existing = None
        checked_plan = None
        ledger_service = None
        if not defer_preflight:
            # Direct desktop requests keep their fail-fast behaviour. Formal
            # cloud tasks defer these potentially slow HubStudio reads to the
            # worker so the loopback acceptance request cannot time out while
            # work continues invisibly in the background.
            require_envbatch_ready(
                hub, runtime['purchaseTag'], runtime['proxyLink'],
                site=runtime['site'])
            selected_existing = hub.env_list(runtime['purchaseTag'])
            all_existing = hub.env_list()
            checked_plan = build_batch_plan(
                pending['accounts'], assignment,
                existing_envs=selected_existing,
                site=runtime['site'], purchase_date=purchase_date,
                all_existing_envs=all_existing,
                reject_existing_account_refs=cleanup_blocked_account_refs,
                planned_env_names=planned_name_map)
            if reserve_resources:
                reserve_resources(environment_resources(checked_plan))
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
            self.stop_requested = False
            self.stop_available = True
            self.stop_event = threading.Event()
            stop_event = self.stop_event
            self.started_at = time.time()
            self.finished_at = None
            self.rows = []
            self.summary = {}
            self.ip_checks = []
            self.phase = 'preparing'
            self.ip_check_total = 0
            self.fatal_error = ''
            self.fatal_error_code = ''
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
            active_ledger_service = ledger_service
            try:
                if defer_preflight:
                    require_envbatch_ready(
                        hub, runtime['purchaseTag'], runtime['proxyLink'],
                        site=runtime['site'])
                runner = BatchEnvOrchestrator(
                    hub, purchase_tag=runtime['purchaseTag'],
                    proxy_link=runtime['proxyLink'], site=runtime['site'],
                    purchase_date=purchase_date,
                    state_store=ResumeStateStore(batch_id),
                    on_progress=self._set_rows,
                    max_workers=runtime['workers'],
                    stop_event=stop_event,
                    reject_existing_account_refs=cleanup_blocked_account_refs)
                with self.lock:
                    self.runner = runner
                runner.prepare(
                    accounts, assignment,
                    existing_envs=selected_existing,
                    all_existing_envs=all_existing,
                    planned_env_names=planned_name_map,
                    trust_cloud_inventory=trust_cloud_inventory)
                if reserve_resources and defer_preflight:
                    reserve_resources(environment_resources(runner.rows))
                if write_lark_ledger and defer_preflight:
                    active_ledger_service = self.ledger_sync_factory()
                    ledger_preflight = active_ledger_service.preflight_plan(
                        runner.rows, runtime['site'], runtime['purchaseTag'])
                    if ledger_preflight.get('conflicts'):
                        raise ValueError(
                            '飞书统一台账发现 %d 条双键或站点冲突，已阻止建环境' %
                            ledger_preflight['conflicts'])
                    with self.lock:
                        self._ledger_service = active_ledger_service
                with self.lock:
                    self.phase = 'creating'
                result_rows = runner.run()
                if stop_event.is_set():
                    with self.lock:
                        self.phase = 'rolling_back'
                    runner.rollback_created_environments()
                    result_rows = runner.rows
                mapping = mapping_workbook_bytes(result_rows)
                verification_total = min(
                    verify_sample_count,
                    sum(row.state == 'done' and bool(row.container_code)
                        for row in result_rows))
                with self.lock:
                    self.phase = ('ip_checking'
                                  if verification_total else 'finalizing')
                    self.ip_check_total = verification_total
                checks = ([] if stop_event.is_set()
                          else runner.verify_ips(
                              verify_sample_count,
                              on_progress=self._set_ip_checks))
                done = sum(row.state == 'done' for row in result_rows)
                stopped = sum(
                    row.state in ('stopped', 'rolled_back')
                    for row in result_rows)
                failed = sum(
                    row.state in ('failed', 'cleanup_failed')
                    for row in result_rows)
                cleanup_total = sum(
                    row.created_in_run for row in result_rows)
                cleanup_done = sum(
                    row.cleanup_status == 'deleted' for row in result_rows)
                cleanup_failed = sum(
                    row.cleanup_status == 'failed' for row in result_rows)
                if write_lark_ledger:
                    self._sync_ledger_rows(
                        active_ledger_service, result_rows,
                        runtime['site'], purchase_date,
                        runtime['purchaseTag'])
                with self.lock:
                    self.mapping_data = mapping
                    self.mapping_name = '绑定映射清单_%s.xlsx' % purchase_date
                    # 测试版不再为 Web 运行链路生成含凭证的旧台账 TSV。
                    self.tsv_data = None
                    self.tsv_name = ''
                    self.ip_checks = checks
                    self.phase = 'finalizing'
                    self.summary = {
                        'total': len(result_rows),
                        'done': done,
                        'stopped': stopped,
                        'failed': failed,
                        'ipOk': sum(bool(item.get('ok')) for item in checks),
                        'ipTotal': len(checks),
                        'cleanupTotal': cleanup_total,
                        'cleanupDone': cleanup_done,
                        'cleanupFailed': cleanup_failed,
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
                    self.fatal_error_code = self._fatal_reason(exc)
            finally:
                if self.fatal_error:
                    self._clear_sensitive(runner=runner)
                with self.lock:
                    self.finished_at = time.time()
                    self.running = False
                    self.stop_available = False
                    self.phase = ('failed' if self.fatal_error else
                                  'stopped' if self.stop_requested else
                                  'completed')
                if on_finished:
                    try:
                        on_finished()
                    except Exception:
                        pass

        threading.Thread(target=worker, daemon=True).start()
        return len(accounts)

    def request_stop(self):
        """Request a cooperative stop without interrupting an active row."""
        with self.lock:
            if not self.running or not self.stop_available:
                raise RuntimeError('没有正在执行且可安全停止的买家号建环境任务')
            self.stop_requested = True
            self.stop_event.set()
            return {
                'stopping': True,
                'stopRequested': True,
                'message': '已停止领取新行；当前并发行收尾后将销毁本任务新建环境',
            }

    def retry_row(self, account_id, reserve_resources=None,
                  on_finished=None):
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
            if reserve_resources:
                reserve_resources(environment_resources([row]))
            self._cancel_sensitive_cleanup_locked()
            self.running = True
            self.stop_requested = False
            self.stop_available = True
            self.stop_event = threading.Event()
            self.runner.stop_event = self.stop_event
            self.started_at = time.time()
            self.finished_at = None
            self.phase = 'creating'
            self.fatal_error = ''
            self.fatal_error_code = ''

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
                    self.tsv_data = None
                    self.tsv_name = ''
                    self.summary.update({
                        'total': len(result_rows),
                        'done': sum(row.state == 'done' for row in result_rows),
                        'stopped': sum(
                            row.state == 'stopped' for row in result_rows),
                        'failed': sum(
                            row.state == 'failed' for row in result_rows),
                    })
                self._schedule_sensitive_cleanup(self.runner)
            except Exception as exc:
                accounts = [row.account for row in self.runner.rows]
                with self.lock:
                    self.fatal_error = self._safe_error(exc, accounts)
                    self.fatal_error_code = self._fatal_reason(exc)
            finally:
                with self.lock:
                    self.finished_at = time.time()
                    self.running = False
                    self.stop_available = False
                    self.phase = ('failed' if self.fatal_error else
                                  'stopped' if self.stop_requested else
                                  'completed')
                if on_finished:
                    try:
                        on_finished()
                    except Exception:
                        pass

        threading.Thread(target=worker, daemon=True).start()

    def retry_failed(self, reserve_resources=None, on_finished=None):
        with self.lock:
            if self.running:
                raise RuntimeError('模块三任务正在执行')
            if not self.runner:
                raise ValueError('没有可重试的模块三任务')
            failed_rows = [
                row for row in self.runner.rows if row.state == 'failed']
            if not failed_rows:
                raise ValueError('当前批次没有失败行可重试')
            if any(not row.account.password for row in failed_rows):
                raise ValueError('凭证内存已清理，请重新选择原始 xlsx 后续跑')
            if reserve_resources:
                reserve_resources(environment_resources(failed_rows))
            account_ids = [row.account.account_id for row in failed_rows]
            self._cancel_sensitive_cleanup_locked()
            self.running = True
            self.stop_requested = False
            self.stop_available = True
            self.stop_event = threading.Event()
            self.runner.stop_event = self.stop_event
            self.started_at = time.time()
            self.finished_at = None
            self.fatal_error = ''
            self.fatal_error_code = ''
            self.phase = 'creating'

        def worker():
            try:
                self.runner.retry_failed()
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
                    self.tsv_data = None
                    self.tsv_name = ''
                    self.summary.update({
                        'total': len(result_rows),
                        'done': sum(
                            row.state == 'done' for row in result_rows),
                        'stopped': sum(
                            row.state == 'stopped' for row in result_rows),
                        'failed': sum(
                            row.state == 'failed' for row in result_rows),
                    })
                self._schedule_sensitive_cleanup(self.runner)
            except Exception as exc:
                accounts = [row.account for row in self.runner.rows]
                with self.lock:
                    self.fatal_error = self._safe_error(exc, accounts)
                    self.fatal_error_code = self._fatal_reason(exc)
            finally:
                with self.lock:
                    self.finished_at = time.time()
                    self.running = False
                    self.stop_available = False
                    self.phase = ('failed' if self.fatal_error else
                                  'stopped' if self.stop_requested else
                                  'completed')
                if on_finished:
                    try:
                        on_finished(account_ids)
                    except Exception:
                        pass

        threading.Thread(target=worker, daemon=True).start()
        return len(failed_rows)

    def retry_ledger(self, confirm_lark_write=False, on_finished=None):
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
                if on_finished:
                    try:
                        on_finished()
                    except Exception:
                        pass

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
                'stopRequested': self.stop_requested,
                'stopAvailable': self.stop_available,
                'elapsedSec': elapsed,
                'rows': [dict(row) for row in self.rows],
                'summary': dict(self.summary),
                'ipChecks': [dict(item) for item in self.ip_checks],
                'phase': self.phase,
                'ipCheckDone': len(self.ip_checks),
                'ipCheckTotal': self.ip_check_total,
                'fatalErrorCode': self.fatal_error_code,
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
        self.stop_requested = False
        self.stop_event = threading.Event()
        self.started_at = None
        self.finished_at = None
        self.rows = []
        self.summary = {}
        self.ip_checks = []
        self.phase = 'idle'
        self.ip_check_total = 0
        self.fatal_error = ''
        self.result_data = None
        self.result_name = ''

    def _runtime_config(self, site='MX', environment_group=None):
        site = normalize_env_site(site)
        cfg = dict(self.config_getter() or {})
        configured_workers, workers = environment_worker_policy(cfg)
        return {
            'site': site,
            'purchaseTag': validate_purchase_group_site(
                environment_group
                if environment_group is not None
                else purchase_tag_for_site(cfg, site),
                site),
            'proxyLink': effective_proxy_link(cfg),
            'workers': workers,
            'configuredWorkers': configured_workers,
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

    def preview(self, buyer, count, backup_type, purchase_date, site='MX',
                environment_group=None):
        buyer, count, backup_type, purchase_date = self._validate_params(
            buyer, count, backup_type, purchase_date)
        runtime = self._runtime_config(site, environment_group)
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

    def _set_ip_checks(self, checks):
        with self.lock:
            self.ip_checks = [dict(item) for item in checks]

    def start(self, buyer, count, backup_type, purchase_date,
              verify_sample_count=1, confirm_write=False, site='MX',
              environment_group=None,
              reserve_resources=None, on_finished=None):
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
        runtime = self._runtime_config(site, environment_group)
        hub = self.hub_getter()
        # 预检先于启动线程：预检失败零写入。
        require_envbatch_ready(
            hub, runtime['purchaseTag'], runtime['proxyLink'],
            site=runtime['site'])
        planned_names = backup_env_names(
            hub.env_list(runtime['purchaseTag']), buyer, count,
            backup_type, runtime['site'], purchase_date)
        if reserve_resources:
            reserve_resources(environment_resources(
                [{'envName': name} for name in planned_names]))
        with self.lock:
            if self.running:
                raise RuntimeError('已有备用环境任务在进行')
            self.running = True
            self.stop_requested = False
            self.stop_event = threading.Event()
            stop_event = self.stop_event
            self.started_at = time.time()
            self.finished_at = None
            self.rows = []
            self.summary = {}
            self.ip_checks = []
            self.phase = 'preparing'
            self.ip_check_total = 0
            self.fatal_error = ''
            self.result_data = None
            self.result_name = ''

        def worker():
            try:
                runner = BackupEnvOrchestrator(
                    hub, purchase_tag=runtime['purchaseTag'],
                    proxy_link=runtime['proxyLink'], site=runtime['site'],
                    on_progress=self._set_rows,
                    max_workers=runtime['workers'],
                    stop_event=stop_event)
                runner.prepare(buyer, count, backup_type, purchase_date)
                if reserve_resources:
                    reserve_resources(environment_resources(runner.rows))
                with self.lock:
                    self.phase = 'creating'
                result_rows = runner.run()
                if stop_event.is_set():
                    with self.lock:
                        self.phase = 'rolling_back'
                    runner.rollback_created_environments()
                    result_rows = runner.rows
                verification_total = min(
                    verify_sample_count,
                    sum(row.state == 'done' and bool(row.container_code)
                        for row in result_rows))
                with self.lock:
                    self.phase = ('ip_checking'
                                  if verification_total else 'finalizing')
                    self.ip_check_total = verification_total
                checks = ([] if stop_event.is_set()
                          else runner.verify_ips(
                              verify_sample_count,
                              on_progress=self._set_ip_checks))
                done = sum(row.state == 'done' for row in result_rows)
                stopped = sum(
                    row.state in ('stopped', 'rolled_back')
                    for row in result_rows)
                failed = sum(
                    row.state in ('failed', 'cleanup_failed')
                    for row in result_rows)
                cleanup_total = sum(
                    row.created_in_run for row in result_rows)
                cleanup_done = sum(
                    row.cleanup_status == 'deleted' for row in result_rows)
                cleanup_failed = sum(
                    row.cleanup_status == 'failed' for row in result_rows)
                with self.lock:
                    self.ip_checks = checks
                    self.phase = 'finalizing'
                    self.summary = {
                        'total': len(result_rows),
                        'done': done,
                        'stopped': stopped,
                        'failed': failed,
                        'ipOk': sum(bool(item.get('ok')) for item in checks),
                        'ipTotal': len(checks),
                        'cleanupTotal': cleanup_total,
                        'cleanupDone': cleanup_done,
                        'cleanupFailed': cleanup_failed,
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
                    self.phase = ('failed' if self.fatal_error else
                                  'stopped' if self.stop_requested else
                                  'completed')
                if on_finished:
                    try:
                        on_finished()
                    except Exception:
                        pass

        threading.Thread(target=worker, daemon=True).start()
        return count

    def request_stop(self):
        """Request a cooperative stop without interrupting an active row."""
        with self.lock:
            if not self.running:
                raise RuntimeError('没有正在执行的备用/测试环境任务')
            self.stop_requested = True
            self.stop_event.set()
            return {
                'stopping': True,
                'stopRequested': True,
                'message': '已停止领取新行；当前并发行收尾后将销毁本任务新建环境',
            }

    def snapshot(self):
        with self.lock:
            end_at = time.time() if self.running else self.finished_at
            elapsed = int(max(0, end_at - self.started_at)) \
                if self.started_at and end_at else 0
            return {
                'running': self.running,
                'stopRequested': self.stop_requested,
                'elapsedSec': elapsed,
                'rows': [dict(row) for row in self.rows],
                'summary': dict(self.summary),
                'ipChecks': [dict(item) for item in self.ip_checks],
                'phase': self.phase,
                'ipCheckDone': len(self.ip_checks),
                'ipCheckTotal': self.ip_check_total,
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

    def _purchase_assistant_origin(self):
        origin = str(self.headers.get('Origin') or '').strip().lower()
        return origin if re.fullmatch(
            r'chrome-extension://[a-p]{32}', origin) else ''

    def _purchase_assistant_json(self, obj, status=200):
        origin = self._purchase_assistant_origin()
        headers = {'Vary': 'Origin'}
        if origin:
            headers['Access-Control-Allow-Origin'] = origin
        self._json(obj, status, headers)

    def _purchase_assistant_pair_allowed(self):
        peer = str((self.client_address or ('',))[0]).casefold()
        host = str(self.headers.get('Host') or '').partition(':')[0].lower()
        raw_origin = str(self.headers.get('Origin') or '').strip()
        origin_allowed = (
            not raw_origin or bool(self._purchase_assistant_origin()))
        return (
            peer in {'127.0.0.1', '::1'}
            and host in {'127.0.0.1', 'localhost', 'xynigo.localhost'}
            and origin_allowed
            and self.headers.get('X-Xynigo-Client') == 'chrome-extension'
            and self.headers.get('X-Xynigo-Pairing') == 'auto'
        )

    def _purchase_assistant_request_allowed(self):
        peer = str((self.client_address or ('',))[0]).casefold()
        host = str(self.headers.get('Host') or '').partition(':')[0].lower()
        raw_origin = str(self.headers.get('Origin') or '').strip()
        return (
            peer in {'127.0.0.1', '::1'}
            and host in {'127.0.0.1', 'localhost', 'xynigo.localhost'}
            and (not raw_origin or bool(self._purchase_assistant_origin()))
            and self.headers.get('X-Xynigo-Client') == 'chrome-extension'
        )

    def _handle_purchase_assistant_get(self, parsed):
        path = parsed.path
        bridge = STATE.purchase_assistant
        if path == PURCHASE_ASSISTANT_API_PREFIX + '/health':
            configured = bool(bridge.configured)
            try:
                registry = STATE.data_sources.snapshot()['registry']
                configured = any(
                    item.get('enabled')
                    and item.get('migrationState') == 'ready'
                    for item in registry.get('dataSources') or [])
            except Exception:
                pass
            return self._purchase_assistant_json({
                'ok': True,
                'service': 'xynigo-sourcing',
                'apiVersion': 4,
                'features': {
                    'taskSearch': True,
                    'recipientRead': True,
                    'sourceConfiguration': False,
                    'desktopManagedDataSources': True,
                    'memberScopedDataSources': True,
                    'environmentScopedDataSources': False,
                    'hubStudioAutomation': True,
                    'hubStudioEnvironmentControl': True,
                },
                'version': __version__,
                'configured': configured,
                'settingsUrl': 'xynigo://settings',
            })
        if path == PURCHASE_ASSISTANT_API_PREFIX + '/session':
            if not self._purchase_assistant_pair_allowed():
                return self._purchase_assistant_json({
                    'ok': False,
                    'code': 'pairing_denied',
                    'error': '仅允许本机采购助手自动配对',
                }, 403)
            return self._purchase_assistant_json({
                'ok': True,
                'sessionToken': bridge.issue_session(),
            })
        if not bridge.authorize(self.headers.get('Authorization')):
            return self._purchase_assistant_json({
                'ok': False,
                'code': 'session_required',
                'error': '采购助手本次会话已失效',
            }, 401)
        if not self._purchase_assistant_request_allowed():
            return self._purchase_assistant_json({
                'ok': False,
                'code': 'origin_forbidden',
                'error': '采购助手请求来源无效',
            }, 403)
        try:
            identity = STATE.auth.require()
            member_id = identity['user']['id']
            if path == PURCHASE_ASSISTANT_API_PREFIX + '/capabilities':
                return self._purchase_assistant_json({
                    'ok': True,
                    'hubStudio': STATE.hub_capabilities(force=True),
                })
            if path == PURCHASE_ASSISTANT_API_PREFIX + '/data-source':
                query = parse_qs(parsed.query, keep_blank_values=True)
                container_code = str(
                    (query.get('containerCode') or [''])[0]).strip()
                _service, source_status = \
                    STATE.purchase_assistant_for_member(
                        member_id, container_code=container_code)
                return self._purchase_assistant_json({
                    'ok': True,
                    'source': public_purchase_assistant_source_context(
                        identity, source_status, container_code),
                })
            if path == PURCHASE_ASSISTANT_API_PREFIX + '/tasks':
                query = parse_qs(parsed.query, keep_blank_values=True)
                keyword = str((query.get('query') or [''])[0]).strip()
                container_code = str(
                    (query.get('containerCode') or [''])[0]).strip()
                if len(keyword) > 100:
                    raise PurchaseAssistantError('任务搜索条件过长')
                service, _source_status = \
                    STATE.purchase_assistant_for_member(
                        member_id, container_code=container_code)
                if not keyword:
                    return self._purchase_assistant_json({
                        'ok': True,
                        'tasks': [],
                        'total': 0,
                        'queryRequired': True,
                    })
                matched, total = service.search(keyword, limit=20)
                return self._purchase_assistant_json({
                    'ok': True,
                    'tasks': matched,
                    'total': total,
                    'queryRequired': False,
                    'truncated': total > len(matched),
                    'source': public_purchase_assistant_source_context(
                        identity, _source_status, container_code),
                })
            prefix = PURCHASE_ASSISTANT_API_PREFIX + '/tasks/'
            suffix = '/recipient'
            if path.startswith(prefix) and path.endswith(suffix):
                encoded = path[len(prefix):-len(suffix)]
                key = str(unquote(encoded)).strip()
                if not key or len(key) > 300:
                    raise PurchaseAssistantError('采购任务标识无效')
                query = parse_qs(parsed.query, keep_blank_values=True)
                container_code = str(
                    (query.get('containerCode') or [''])[0]).strip()
                service, _source_status = \
                    STATE.purchase_assistant_for_member(
                        member_id, container_code=container_code)
                return self._purchase_assistant_json({
                    'ok': True,
                    'recipient': service.recipient(key),
                    'source': public_purchase_assistant_source_context(
                        identity, _source_status, container_code),
                })
            if path == PURCHASE_ASSISTANT_API_PREFIX + '/hub/environments':
                query = parse_qs(parsed.query, keep_blank_values=True)
                keyword = str((query.get('query') or [''])[0]).strip()
                try:
                    limit = int((query.get('limit') or ['100'])[0])
                except (TypeError, ValueError):
                    raise PurchaseAssistantError('环境列表数量参数无效')
                return self._purchase_assistant_json({
                    'ok': True,
                    'environments': STATE.hub.list_environment_summaries(
                        keyword, limit=limit),
                })
            if path == (PURCHASE_ASSISTANT_API_PREFIX
                        + '/hub/environments/locate'):
                query = parse_qs(parsed.query, keep_blank_values=True)
                identifier = str(
                    (query.get('identifier') or [''])[0]).strip()
                env = STATE.hub.locate_environment(identifier)
                return self._purchase_assistant_json({
                    'ok': True,
                    'environment': STATE.hub.environment_summary(env),
                })
            return self._purchase_assistant_json({
                'ok': False, 'code': 'not_found', 'error': '接口不存在',
            }, 404)
        except LocalAuthError as exc:
            return self._purchase_assistant_json({
                'ok': False,
                'code': exc.code,
                'error': str(exc),
            }, exc.status)
        except LocalConfigRevisionConflict as exc:
            return self._purchase_assistant_json({
                'ok': False,
                'code': exc.code,
                'error': str(exc),
                'configRevision': exc.actual_revision,
            }, 409)
        except DataSourceMappingRequired as exc:
            return self._purchase_assistant_json({
                'ok': False,
                'code': exc.code,
                'error': str(exc),
            }, 409)
        except DataSourceRegistryError as exc:
            return self._purchase_assistant_json({
                'ok': False,
                'code': exc.code,
                'error': str(exc),
            }, 409)
        except PurchaseAssistantError as exc:
            return self._purchase_assistant_json({
                'ok': False,
                'code': 'business_error',
                'error': str(exc),
            }, 422)
        except HubApiError as exc:
            return self._purchase_assistant_json({
                'ok': False,
                'code': exc.reason_code,
                'error': str(exc),
                'hubStudio': STATE.hub_capabilities(force=True),
            }, 503)
        except Exception:
            return self._purchase_assistant_json({
                'ok': False,
                'code': 'internal_error',
                'error': '采购助手执行器内部异常',
            }, 500)

    def _handle_purchase_assistant_post(self, path, body):
        bridge = STATE.purchase_assistant
        if not self._purchase_assistant_request_allowed():
            return self._purchase_assistant_json({
                'ok': False,
                'code': 'origin_forbidden',
                'error': '采购助手请求来源无效',
            }, 403)
        if not bridge.authorize(self.headers.get('Authorization')):
            return self._purchase_assistant_json({
                'ok': False,
                'code': 'session_required',
                'error': '采购助手本次会话已失效',
            }, 401)
        if not isinstance(body, dict):
            return self._purchase_assistant_json({
                'ok': False,
                'code': 'request_invalid',
                'error': '请求数据格式无效',
            }, 400)
        try:
            STATE.auth.require()
            if path.startswith(
                    PURCHASE_ASSISTANT_API_PREFIX + '/data-source/'):
                return self._purchase_assistant_json({
                    'ok': False,
                    'code': 'local_config_desktop_only',
                    'error': '收件信息数据源只能在 Xynigo 桌面客户端配置',
                    'settingsUrl': 'xynigo://settings',
                }, 410)
            capability = STATE.hub_capabilities(force=True)
            if not capability.get('available'):
                return self._purchase_assistant_json({
                    'ok': False,
                    'code': str(capability.get('reasonCode') or
                                'hubstudio_unavailable'),
                    'error': str(capability.get('message') or
                                 'HubStudio 自动化暂不可用'),
                    'hubStudio': capability,
                }, 503)
            if path in {
                    PURCHASE_ASSISTANT_API_PREFIX + '/hub/environments/open',
                    PURCHASE_ASSISTANT_API_PREFIX + '/hub/environments/close'}:
                env = STATE.hub.locate_environment(body.get('identifier'))
                code = str(env.get('containerCode') or '')
                if path.endswith('/open'):
                    STATE.hub.browser_start(code, headless=False)
                    action = 'open'
                else:
                    STATE.hub.browser_stop(code)
                    action = 'close'
                return self._purchase_assistant_json({
                    'ok': True,
                    'action': action,
                    'environment': STATE.hub.environment_summary(env),
                })
            if path == (PURCHASE_ASSISTANT_API_PREFIX
                        + '/hub/environments/batch'):
                results = STATE.hub.batch_browser_control(
                    body.get('action'), body.get('identifiers'),
                    headless=False)
                return self._purchase_assistant_json({
                    'ok': all(item.get('ok') for item in results),
                    'results': results,
                })
            return self._purchase_assistant_json({
                'ok': False, 'code': 'not_found', 'error': '接口不存在',
            }, 404)
        except LocalAuthError as exc:
            return self._purchase_assistant_json({
                'ok': False,
                'code': exc.code,
                'error': str(exc),
            }, exc.status)
        except LocalConfigRevisionConflict as exc:
            return self._purchase_assistant_json({
                'ok': False,
                'code': exc.code,
                'error': str(exc),
                'configRevision': exc.actual_revision,
            }, 409)
        except DataSourceMappingRequired as exc:
            return self._purchase_assistant_json({
                'ok': False,
                'code': exc.code,
                'error': str(exc),
            }, 409)
        except DataSourceRegistryError as exc:
            return self._purchase_assistant_json({
                'ok': False,
                'code': exc.code,
                'error': str(exc),
            }, 409)
        except PurchaseAssistantError as exc:
            return self._purchase_assistant_json({
                'ok': False,
                'code': 'source_invalid',
                'error': str(exc),
            }, 422)
        except HubApiError as exc:
            return self._purchase_assistant_json({
                'ok': False,
                'code': exc.reason_code,
                'error': str(exc),
            }, 422)
        except Exception:
            if path.startswith(
                    PURCHASE_ASSISTANT_API_PREFIX + '/data-source/'):
                return self._purchase_assistant_json({
                    'ok': False,
                    'code': 'source_configuration_failed',
                    'error': '收件信息数据源配置失败',
                }, 500)
            return self._purchase_assistant_json({
                'ok': False,
                'code': 'hubstudio_operation_failed',
                'error': 'HubStudio 操作失败',
            }, 500)

    def _require_auth(self, path):
        if self._internal_executor_rpc_allowed():
            return {
                'user': {'id': 'executor-channel', 'name': '云端执行器'},
                'tenant': {}, 'roles': [], 'permissions': [],
            }
        if path in PUBLIC_AUTH_API_PATHS:
            return None
        if (path.startswith('/api/admin/members/')
                and path.endswith('/roles')):
            return STATE.auth.require('system.role.manage')
        if procurement_write_path(path):
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
        if self._internal_executor_rpc_allowed():
            return
        fetch_site = str(self.headers.get('Sec-Fetch-Site') or '').casefold()
        if fetch_site == 'cross-site':
            raise LocalAuthError(
                'origin_forbidden', '拒绝来自外部网页的本机写入请求', 403)

    def _internal_executor_rpc_allowed(self):
        expected = str(getattr(self.server, 'executor_rpc_token', '') or '')
        submitted = str(self.headers.get('X-Xynigo-Executor-RPC') or '')
        peer = str((self.client_address or ('',))[0]).casefold()
        host = str(self.headers.get('Host') or '').partition(':')[0].casefold()
        origin = str(self.headers.get('Origin') or '').strip()
        return bool(
            expected
            and len(submitted) >= 32
            and secrets.compare_digest(expected, submitted)
            and peer in {'127.0.0.1', '::1'}
            and host in {'127.0.0.1', 'localhost'}
            and not origin
            and self.headers.get('X-Xynigo-Source') == 'executor_workspace_rpc'
        )

    def _operation_run_key(self, body, fallback):
        """Honor cloud Run identity only on the authenticated loopback path."""
        if not self._internal_executor_rpc_allowed():
            return str(fallback or '')
        value = str((body or {}).get('operationRunKey') or '').strip()
        if not value:
            return str(fallback or '')
        if not re.fullmatch(r'[A-Za-z0-9._:-]{8,128}', value):
            raise ValueError('云端业务运行编号无效')
        return value
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

    def _file(self, path, mime, extra_headers=None):
        with open(path, 'rb') as f:
            body = f.read()
        self.send_response(200)
        content_type = (mime + '; charset=utf-8'
                        if mime.startswith('text/') else mime)
        self.send_header('Content-Type', content_type)
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        for name, value in (extra_headers or {}).items():
            self.send_header(name, value)
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

    def _inline_bytes(self, data, mime):
        self.send_response(200)
        self.send_header('Content-Type', mime)
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
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

    def _launcher_control_allowed(self):
        expected = str(os.environ.get('XYNIGO_LAUNCHER_TOKEN') or '')
        submitted = str(self.headers.get('X-Xynigo-Launcher') or '')
        peer = str((self.client_address or ('',))[0])
        return bool(
            expected
            and peer in ('127.0.0.1', '::1')
            and secrets.compare_digest(expected, submitted)
        )

    def _require_launcher_control(self):
        if self._launcher_control_allowed():
            return True
        self._json({
            'error': '本地启动器控制请求无效',
            'code': 'launcher_control_forbidden',
        }, 403)
        return False

    # ---- GET ----

    def do_OPTIONS(self):
        path = urlparse(self.path).path
        if not path.startswith(PURCHASE_ASSISTANT_API_PREFIX + '/'):
            self.send_error(501, 'Unsupported method')
            return
        origin = self._purchase_assistant_origin()
        if not origin:
            self._purchase_assistant_json({
                'ok': False,
                'code': 'origin_forbidden',
                'error': '仅允许浏览器扩展访问',
            }, 403)
            return
        self.send_response(204)
        self.send_header('Access-Control-Allow-Origin', origin)
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header(
            'Access-Control-Allow-Headers',
            'Authorization, Content-Type, X-Xynigo-Client, X-Xynigo-Pairing')
        self.send_header('Access-Control-Max-Age', '600')
        self.send_header('Vary', 'Origin')
        self.send_header('Content-Length', '0')
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        path, query = parsed.path, parse_qs(parsed.query)
        try:
            if path.startswith(PURCHASE_ASSISTANT_API_PREFIX + '/'):
                return self._handle_purchase_assistant_get(parsed)
            if path.startswith('/api/'):
                if (self._internal_executor_rpc_allowed()
                        and (path in LOCAL_CONFIG_RPC_PATHS
                             or path.startswith(DATA_SOURCE_API_PREFIX))):
                    return self._json({
                        'error': '本机配置只能在桌面客户端查看和修改',
                        'code': 'local_config_desktop_only',
                    }, 410)
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
            elif path in {'/desktop', '/desktop/'}:
                self._file(DESKTOP_HTML, 'text/html', {
                    'Content-Security-Policy': (
                        "default-src 'self'; img-src 'self' data:; "
                        "style-src 'self' 'unsafe-inline'; "
                        "script-src 'self'; connect-src 'self'; "
                        "object-src 'none'; frame-src 'none'; "
                        "base-uri 'none'; form-action 'self'"
                    ),
                    'Referrer-Policy': 'no-referrer',
                    'X-Frame-Options': 'DENY',
                })
            elif path == '/desktop.css':
                self._file(DESKTOP_CSS, 'text/css')
            elif path == '/desktop.js':
                self._file(DESKTOP_JS, 'text/javascript')
            elif path == '/executor-status.json':
                payload = STATE.local_executor_status()
                payload['localPort'] = int(self.server.server_port)
                self._json(payload)
            elif path == '/api/auth/status':
                self._json(STATE.auth.status(force=True))
            elif path == '/api/hub-status':
                ok, err = STATE.hub_status(force=True)
                self._json({'connected': ok, 'error': err})
            elif path == '/api/hub-core-repair/status':
                self._json(STATE.hub_core_repair.snapshot())
            elif path == '/api/update/status':
                STATE.updates.check_async()
                self._json(STATE.updates.snapshot())
            elif path == '/api/groups':
                loader = getattr(STATE, 'hub_groups', None)
                groups = loader() if callable(loader) else STATE.hub.group_list()
                self._json({'groups': groups})
            elif path == '/api/workspace/snapshot':
                self._json(STATE.workspace_snapshot())
            elif path == '/api/group-envs':
                group = (query.get('group') or [''])[0]
                loader = getattr(STATE, 'hub_group_serials', None)
                if callable(loader):
                    serials = loader(group or None)
                else:
                    envs = STATE.hub.env_list(group or None)
                    serials = sorted(
                        [str(e.get('serialNumber')) for e in envs
                         if e.get('serialNumber') is not None],
                        key=lambda x: int(x) if x.isdigit() else 0)
                self._json({'serials': serials, 'count': len(serials)})
            elif path == '/api/progress':
                snap = STATE.orch.snapshot()
                snap['hubConnected'] = STATE.hub_status()[0]
                snap['serverSync'] = (
                    STATE.operation_sync.snapshot()
                    if hasattr(STATE, 'operation_sync') else
                    {'pending': 0, 'rows': []})
                self._json(snap)
            elif path == '/api/tasks':
                snap = STATE.tasks.snapshot()
                snap['serverSync'] = (
                    STATE.operation_sync.snapshot()
                    if hasattr(STATE, 'operation_sync') else
                    {'pending': 0, 'rows': []})
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
            elif path == '/api/assistant/procurement-import/export':
                data, name, mime = STATE.procurement_import.export(
                    (query.get('planId') or [''])[0])
                self._download(data, name, mime)
            elif path == '/api/assistant/procurement-import/image':
                data, mime = STATE.procurement_import.preview_image(
                    (query.get('planId') or [''])[0],
                    (query.get('row') or [''])[0])
                self._inline_bytes(data, mime)
            elif path == '/api/assistant/procurement-import/image-sync/status':
                self._json(STATE.procurement_import.image_sync_status(
                    (query.get('jobId') or [''])[0]))
            elif path == '/api/assistant/procurement-import/sheet-sync/status':
                self._json(STATE.procurement_import.sheet_sync_status(
                    (query.get('jobId') or [''])[0]))
            elif path == '/api/cloud/buyer-accounts':
                cloud_path = '/v1/resources/buyer-accounts'
                if parsed.query:
                    cloud_path += '?' + parsed.query
                self._json(STATE.auth.buyer_account_request(cloud_path))
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
                snap['serverSync'] = (
                    STATE.operation_sync.snapshot()
                    if hasattr(STATE, 'operation_sync') else
                    {'pending': 0, 'rows': []})
                self._json(snap)
            elif path == '/api/envbatch/preflight':
                site = (query.get('site') or ['MX'])[0]
                environment_group = (
                    query.get('environmentGroup') or [None])[0]
                self._json(
                    STATE.env_job.preflight(
                        site, environment_group=environment_group)
                    if environment_group is not None
                    else STATE.env_job.preflight(site))
            elif path == '/api/envbatch/preferences':
                self._json(public_envbatch_preferences(STATE.cfg))
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
                self._json({
                    'error': '旧统一台账直贴导出已停用；买家号状态由数据库同步到独立 Base',
                }, 410)
            elif path == '/api/envbatch/backup/preview':
                result = STATE.backup_job.preview(
                    (query.get('buyer') or [''])[0],
                    (query.get('count') or [''])[0],
                    (query.get('type') or [''])[0],
                    (query.get('purchaseDate') or
                     [time.strftime('%Y%m%d')])[0],
                    site=(query.get('site') or ['MX'])[0],
                    environment_group=(
                        query.get('environmentGroup') or [None])[0])
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
                include_screenshots = str(
                    (query.get('includeScreenshots') or ['true'])[0]
                ).strip().casefold() not in ('0', 'false', 'no')
                rows = STATE.orch.snapshot()['rows']
                if not rows:
                    return self._json({'error': '还没有查询结果'}, 400)
                data, name, mime = export_bytes(
                    rows, fmt, STATE.orch.screenshot_bytes,
                    include_screenshots=include_screenshots)
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
                self._json(public_local_config(
                    STATE.cfg, getattr(STATE, 'hub_api_key_store', None)))
            elif path == DATA_SOURCE_API_PREFIX:
                identity = STATE.auth.require()
                include_all = bool(set(identity.get('roles') or []) & {
                    'admin', 'super_admin'})
                self._json(STATE.data_sources.public_snapshot(
                    identity['user']['id'], include_all=include_all))
            elif path == DATA_SOURCE_API_PREFIX + '/environment-options':
                identity = STATE.auth.require()
                if not set(identity.get('roles') or []) & {
                        'admin', 'super_admin'}:
                    raise LocalAuthError('permission_denied', status=403)
                search = str((query.get('query') or [''])[0]).strip()
                if len(search) > 160:
                    raise ValueError('环境搜索条件过长')
                environments = STATE.hub.list_environment_summaries(
                    search, limit=(query.get('limit') or ['200'])[0])
                self._json({
                    'environments': environments,
                    'count': len(environments),
                })
            elif path == '/api/lark/status':
                self._json({
                    'ready': False,
                    'managedInCloud': True,
                    'ledgerTargetConfigured': False,
                })
            elif path == '/api/lark/config':
                try:
                    legacy_present = STATE.lark_credentials.load() is not None
                except Exception:
                    legacy_present = False
                self._json({
                    'managedInCloud': True,
                    'legacyCredentialPresent': legacy_present,
                    'message': '企业应用凭证已迁移为云端组织级配置',
                })
            elif path == '/api/lark/open-target':
                self._redirect(lark_target_link(STATE.cfg))
            elif path == '/api/lark/target-url':
                self._json({'url': lark_target_link(STATE.cfg)})
            elif path == '/api/business-logs' or path.startswith(
                    '/api/business-logs/'):
                cloud_path = '/v1/business-logs' + path[
                    len('/api/business-logs'):]
                if parsed.query:
                    cloud_path += '?' + parsed.query
                self._json(STATE.auth.business_log_request(cloud_path))
            elif path == '/api/system-logs' or path.startswith(
                    '/api/system-logs/'):
                cloud_path = '/v1/system-logs' + path[
                    len('/api/system-logs'):]
                if parsed.query:
                    cloud_path += '?' + parsed.query
                self._json(STATE.auth.system_log_request(cloud_path))
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
        except HubApiError as e:
            self._json({
                'error': str(e),
                'code': e.reason_code,
            }, 503)
        except DataSourceRegistryError as e:
            self._json({'error': str(e), 'code': e.code}, 409)
        except ValueError as e:
            self._json({'error': str(e)}, 400)
        except Exception as e:
            self._json({'error': public_error(e)}, 500)

    # ---- POST ----

    def do_POST(self):
        path = urlparse(self.path).path
        request_identity = None
        try:
            if path.startswith('/executor-control/'):
                if not self._require_launcher_control():
                    return
            if path == '/executor-control/shutdown':
                self._json({'stopping': True})
                threading.Thread(
                    target=self.server.shutdown,
                    name='xynigo-launcher-shutdown',
                    daemon=True,
                ).start()
                return
            if path == '/executor-control/ping':
                self._json({'accepted': True})
                return
            if path == '/executor-control/update/check':
                started = STATE.updates.check_async(force=True)
                payload = STATE.updates.snapshot()
                payload['started'] = started
                self._json(payload, 202 if started else 200)
                return
            if path == '/executor-control/update/install':
                snapshot = STATE.updates.snapshot()
                if snapshot.get('installMode') != 'standard':
                    return self._json({
                        'error': '绿色版不支持桌面静默升级，请安装标准版',
                        'code': 'standard_installer_required',
                    }, 409)
                active_tasks = STATE.tasks.snapshot().get('tasks') or []
                if active_tasks:
                    return self._json({
                        'error': '本机仍有任务正在执行，请等待任务完成后再更新',
                        'code': 'executor_tasks_active',
                    }, 409)
                accepted = STATE.updates.prompt_async()
                payload = STATE.updates.snapshot()
                payload['accepted'] = accepted
                if not accepted:
                    payload['error'] = '当前没有可安装的新版本'
                    return self._json(payload, 409)
                self._json(payload, 202)
                return
            large_body_paths = {
                '/api/assistant/procurement-import/parse',
                '/api/buyer-library/import/parse',
                '/api/envbatch/parse',
                '/api/envbatch/cloud-plan',
                '/api/register/validate',
            }
            body = self._body(
                max_bytes=(28 * 1024 * 1024)
                if path in large_body_paths else 2 * 1024 * 1024)
            if path.startswith(PURCHASE_ASSISTANT_API_PREFIX + '/'):
                return self._handle_purchase_assistant_post(path, body)
            if path.startswith('/api/extension/v1/'):
                return self._handle_extension_post(path, body)
            if path.startswith('/api/'):
                self._require_same_origin()
                if (self._internal_executor_rpc_allowed()
                        and (path in LOCAL_CONFIG_RPC_PATHS
                             or path.startswith(DATA_SOURCE_API_PREFIX))):
                    return self._json({
                        'error': '本机配置只能在桌面客户端查看和修改',
                        'code': 'local_config_desktop_only',
                    }, 410)
                request_identity = self._require_auth(path)
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
            elif path == '/api/hub-api-key':
                self._json(STATE.save_hub_api_key(
                    value=body.get('apiKey'),
                    clear=bool(body.get('clear'))))
            elif path == '/api/hub-core-repair/start':
                self._json(
                    STATE.hub_core_repair.start(actor=request_identity), 202)
            elif path.startswith('/api/admin/'):
                cloud_path, cloud_method = admin_cloud_write_target(path)
                self._json(STATE.auth.admin_request(
                    cloud_path, method=cloud_method, payload=body))
            elif path == '/api/cloud/buyer-accounts/snapshot':
                self._json(STATE.auth.buyer_account_request(
                    '/v1/resources/buyer-accounts/snapshot',
                    method='PUT',
                    payload=body,
                    permission='resource.buyer.import'))
            elif procurement_write_path(path):
                cloud_path = '/v1/procurement/' + path[len('/api/procurement/'):]
                self._json(STATE.auth.procurement_workspace_request(
                    cloud_path,
                    method='POST',
                    payload=body,
                    permission='procurement.execution.manage'))
            elif path == '/api/query':
                serials = body.get('serials')
                group = body.get('group')
                site = normalize_site(body.get('site') or 'MX')
                allow_open_environment = bool(
                    body.get('allowOpenEnvironment'))
                requested_browser_mode = str(
                    body.get('browserMode') or 'default').strip().casefold()
                browser_mode = normalize_browser_mode(
                    STATE.cfg.get('queryBrowserMode')
                    if requested_browser_mode == 'default'
                    else requested_browser_mode)
                # 手工输入序号时始终在全部 HubStudio 环境中查找；分组只
                # 服务于“查询整个分组”，不再作为序号查询的资格过滤器。
                envs = STATE.hub.env_list(
                    None if serials else (group or None))
                env_index = {str(e.get('serialNumber')): e for e in envs
                             if e.get('serialNumber') is not None}
                if not serials and group:
                    serials = sorted(
                        [str(e.get('serialNumber')) for e in envs
                         if e.get('serialNumber') is not None],
                        key=lambda x: int(x) if x.isdigit() else 0)
                if not serials:
                    return self._json({'error': '未提供环境序号'}, 400)
                selected_serials = [str(serial) for serial in serials]
                query_mode = str(body.get('queryMode') or 'initial')
                if query_mode not in ('initial', 'failed_retry'):
                    return self._json({'error': '查询模式无效'}, 400)
                STATE.orch.preflight_batch(
                    selected_serials, env_index, site=site,
                    browser_mode=browser_mode,
                    allow_open_environment=allow_open_environment)
                selected_envs = [env_index[str(serial)] for serial in selected_serials
                                 if str(serial) in env_index]
                task_id = STATE.tasks.begin(
                    'query', environment_resources(selected_envs))
                operation_run_key = self._operation_run_key(body, task_id)

                def finish_query():
                    try:
                        payload = STATE.logistics_result_payload(
                            operation_run_key, query_mode, selected_serials)
                        STATE.enqueue_operation_result(
                            '/v1/operations/logistics-query-runs',
                            'fulfillment.order.read', payload)
                    except Exception as exc:
                        if hasattr(STATE, 'operation_sync_error'):
                            STATE.operation_sync_error = scrub_text(exc)[:200]
                    finally:
                        STATE.tasks.finish(task_id)
                try:
                    STATE.orch.start_batch(
                        serials, env_index, site=site,
                        on_finished=finish_query,
                        browser_mode=browser_mode,
                        allow_open_environment=allow_open_environment)
                except Exception:
                    STATE.tasks.finish(task_id)
                    raise
                self._json({'started': True, 'total': len(serials),
                            'site': site, 'taskId': task_id,
                            'browserMode': browser_mode,
                            'allowOpenEnvironment': allow_open_environment})
            elif path == '/api/stop':
                STATE.orch.request_stop()
                self._json({'stopped': True})
            elif path == '/api/requery':
                serial = str(body.get('serial') or '')
                if not serial:
                    return self._json({'error': '缺少 serial'}, 400)
                env = STATE.hub.env_by_serial(serial)
                env_index = {serial: env} if env else {}
                task_id = STATE.tasks.begin(
                    'query', environment_resources([env] if env else []))
                operation_run_key = self._operation_run_key(body, task_id)
                requested_browser_mode = str(
                    body.get('browserMode') or 'default').strip().casefold()
                browser_mode = normalize_browser_mode(
                    STATE.cfg.get('queryBrowserMode')
                    if requested_browser_mode == 'default'
                    else requested_browser_mode)
                allow_open_environment = bool(
                    body.get('allowOpenEnvironment'))

                def finish_requery():
                    try:
                        payload = STATE.logistics_result_payload(
                            operation_run_key, 'single_retry', [serial])
                        STATE.enqueue_operation_result(
                            '/v1/operations/logistics-query-runs',
                            'fulfillment.order.read', payload)
                    except Exception as exc:
                        if hasattr(STATE, 'operation_sync_error'):
                            STATE.operation_sync_error = scrub_text(exc)[:200]
                    finally:
                        STATE.tasks.finish(task_id)
                try:
                    STATE.orch.requery(
                        serial, env_index=env_index,
                        force=bool(body.get('force')),
                        on_finished=finish_requery,
                        site=body.get('site'),
                        allow_missing=bool(body.get('operationRunKey')),
                        browser_mode=browser_mode,
                        allow_open_environment=allow_open_environment)
                except Exception:
                    STATE.tasks.finish(task_id)
                    raise
                self._json({'started': True, 'taskId': task_id})
            elif path == '/api/requery-failed':
                rows = STATE.orch.snapshot().get('rows') or []
                retry_serials = [str(row.get('serial')) for row in rows
                                 if row.get('state') in (
                                     'fail', 'inuse', 'pending', 'stopped')]
                all_envs = STATE.hub.env_list()
                env_index = {str(e.get('serialNumber')): e for e in all_envs
                             if e.get('serialNumber') is not None}
                selected = [env_index[serial] for serial in retry_serials
                            if serial in env_index]
                task_id = STATE.tasks.begin(
                    'query', environment_resources(selected))
                operation_run_key = self._operation_run_key(body, task_id)
                requested_browser_mode = str(
                    body.get('browserMode') or 'default').strip().casefold()
                browser_mode = normalize_browser_mode(
                    STATE.cfg.get('queryBrowserMode')
                    if requested_browser_mode == 'default'
                    else requested_browser_mode)
                allow_open_environment = bool(
                    body.get('allowOpenEnvironment'))

                def finish_failed_requery():
                    try:
                        payload = STATE.logistics_result_payload(
                            operation_run_key, 'failed_retry', retry_serials)
                        STATE.enqueue_operation_result(
                            '/v1/operations/logistics-query-runs',
                            'fulfillment.order.read', payload)
                    except Exception as exc:
                        if hasattr(STATE, 'operation_sync_error'):
                            STATE.operation_sync_error = scrub_text(exc)[:200]
                    finally:
                        STATE.tasks.finish(task_id)
                try:
                    count = STATE.orch.requery_failed(
                        env_index=env_index,
                        on_finished=finish_failed_requery,
                        browser_mode=browser_mode,
                        allow_open_environment=allow_open_environment)
                except Exception:
                    STATE.tasks.finish(task_id)
                    raise
                self._json({'started': True, 'count': count,
                            'taskId': task_id})
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
                if body.get('writeLarkLedger'):
                    raise ValueError(
                        '测试版已停用旧买家号台账直写；结果应先写数据库再同步 Base')
                task_id = STATE.tasks.begin('register')
                try:
                    count = STATE.reg_job.start(
                        body.get('tasks'),
                        accept_terms=bool(body.get('acceptTerms')),
                        acknowledge_ms_privacy=bool(
                            body.get('acknowledgeMsPrivacy')),
                        keep_open=bool(body.get('keepOpen')),
                        write_lark_ledger=bool(body.get('writeLarkLedger')),
                        confirm_lark_write=bool(body.get('confirmLarkWrite')),
                        on_finished=lambda: STATE.tasks.finish(task_id))
                except Exception:
                    STATE.tasks.finish(task_id)
                    raise
                self._json({'started': True, 'count': count,
                            'taskId': task_id})
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
            elif path == '/api/assistant/procurement-import/parse':
                self._json(STATE.procurement_import.parse(
                    body.get('filename'), body.get('contentBase64')))
            elif path == '/api/assistant/procurement-import/target/inspect':
                self._json(STATE.procurement_import.inspect_target(
                    body.get('planId'), body.get('spreadsheetUrl')))
            elif path == '/api/assistant/procurement-import/target/validate':
                self._json(STATE.procurement_import.validate_target(
                    body.get('planId'), body.get('spreadsheetUrl'),
                    body.get('sheetId')))
            elif path == '/api/assistant/procurement-import/image-sync':
                self._json(STATE.procurement_import.start_image_sync(
                    body.get('planId'),
                    confirm_write=bool(body.get('confirmWrite')),
                    operator_name=(
                        ((request_identity or {}).get('user') or {}).get(
                            'name') or '')), 202)
            elif path == '/api/assistant/procurement-import/sheet-sync':
                self._json(STATE.procurement_import.start_sheet_sync(
                    body.get('planId'),
                    confirm_write=bool(body.get('confirmWrite')),
                    operator_name=(
                        ((request_identity or {}).get('user') or {}).get(
                            'name') or '')), 202)
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
                    body.get('filename'), body.get('contentBase64'),
                    site=body.get('site') or 'MX')
                self._json(result)
            elif path == '/api/envbatch/cloud-plan':
                result = STATE.env_job.import_cloud_plan(
                    body.get('accounts'),
                    site=body.get('site') or 'MX',
                    filename=body.get('filename') or '云端解析计划.xlsx',
                    cloud_plan_id=body.get('cloudPlanId'))
                self._json(result)
            elif path == '/api/envbatch/preview':
                preview = STATE.env_job.preview(
                    body.get('planId'), body.get('assignment'),
                    body.get('purchaseDate') or time.strftime('%Y%m%d'),
                    site=body.get('site') or 'MX',
                    environment_group=body.get('environmentGroup'),
                    include_inventory_snapshot=bool(
                        body.get('includeInventorySnapshot')))
                if isinstance(preview, tuple):
                    rows, inventory_snapshot = preview
                else:
                    rows, inventory_snapshot = preview, None
                result = {'valid': True, 'count': len(rows), 'rows': rows}
                if inventory_snapshot is not None:
                    result['inventorySnapshot'] = inventory_snapshot
                self._json(result)
            elif path == '/api/envbatch/start':
                if body.get('writeLarkLedger'):
                    raise ValueError(
                        '测试版已停用旧买家号台账直写；建环境结果将写数据库并同步新 Base')
                task_id = STATE.tasks.begin('env_batch')
                operation_run_key = self._operation_run_key(body, task_id)

                def finish_environment_batch():
                    try:
                        payload = STATE.environment_result_payload(
                            operation_run_key)
                        STATE.enqueue_operation_result(
                            '/v1/operations/environment-creation-runs',
                            'resource.environment.create', payload)
                    except Exception as exc:
                        if hasattr(STATE, 'operation_sync_error'):
                            STATE.operation_sync_error = scrub_text(exc)[:200]
                    finally:
                        STATE.tasks.finish(task_id)
                try:
                    count = STATE.env_job.start(
                        body.get('planId'), body.get('assignment'),
                        body.get('purchaseDate') or time.strftime('%Y%m%d'),
                        verify_sample_count=body.get('verifySampleCount', 3),
                        confirm_write=bool(body.get('confirmWrite')),
                        site=body.get('site') or 'MX',
                        environment_group=body.get('environmentGroup'),
                        write_lark_ledger=bool(body.get('writeLarkLedger')),
                        confirm_lark_write=bool(body.get('confirmLarkWrite')),
                        reserve_resources=lambda resources:
                            STATE.tasks.reserve(task_id, resources),
                        on_finished=finish_environment_batch,
                        cleanup_blocked_account_refs=body.get(
                            'cleanupBlockedAccountRefs'),
                        defer_preflight=bool(str(
                            body.get('operationRunKey') or '').strip()),
                        planned_environment_names=body.get(
                            'plannedEnvironmentNames'),
                        trust_cloud_inventory=bool(
                            body.get('trustCloudInventory')))
                except Exception:
                    STATE.tasks.finish(task_id)
                    raise
                self._json({'started': True, 'count': count,
                            'taskId': task_id})
            elif path == '/api/envbatch/stop':
                self._json(STATE.env_job.request_stop(), 202)
            elif path == '/api/envbatch/retry-row':
                task_id = STATE.tasks.begin('env_batch')
                account_id = str(body.get('accountId') or '')
                operation_run_key = self._operation_run_key(body, task_id)

                def finish_environment_retry():
                    try:
                        payload = STATE.environment_result_payload(
                            operation_run_key, [account_id])
                        STATE.enqueue_operation_result(
                            '/v1/operations/environment-creation-runs',
                            'resource.environment.create', payload)
                    except Exception as exc:
                        if hasattr(STATE, 'operation_sync_error'):
                            STATE.operation_sync_error = scrub_text(exc)[:200]
                    finally:
                        STATE.tasks.finish(task_id)
                try:
                    STATE.env_job.retry_row(
                        account_id,
                        reserve_resources=lambda resources:
                            STATE.tasks.reserve(task_id, resources),
                        on_finished=finish_environment_retry)
                except Exception:
                    STATE.tasks.finish(task_id)
                    raise
                self._json({'started': True, 'taskId': task_id})
            elif path == '/api/envbatch/retry-failed':
                task_id = STATE.tasks.begin('env_batch')
                operation_run_key = self._operation_run_key(body, task_id)

                def finish_environment_failed_retry(account_ids):
                    try:
                        payload = STATE.environment_result_payload(
                            operation_run_key, account_ids)
                        STATE.enqueue_operation_result(
                            '/v1/operations/environment-creation-runs',
                            'resource.environment.create', payload)
                    except Exception as exc:
                        if hasattr(STATE, 'operation_sync_error'):
                            STATE.operation_sync_error = scrub_text(exc)[:200]
                    finally:
                        STATE.tasks.finish(task_id)
                try:
                    count = STATE.env_job.retry_failed(
                        reserve_resources=lambda resources:
                            STATE.tasks.reserve(task_id, resources),
                        on_finished=finish_environment_failed_retry)
                except Exception:
                    STATE.tasks.finish(task_id)
                    raise
                self._json({'started': True, 'count': count,
                            'taskId': task_id})
            elif path == '/api/envbatch/retry-ledger':
                self._json({
                    'error': '旧买家号台账直写已停用；云端会自动重试数据库到新 Base 的同步',
                }, 410)
            elif path == '/api/envbatch/backup/start':
                task_id = STATE.tasks.begin('backup_env')
                operation_run_key = self._operation_run_key(body, task_id)
                site = body.get('site') or 'MX'
                purchase_date = (
                    body.get('purchaseDate') or time.strftime('%Y%m%d'))
                environment_group = (
                    body.get('environmentGroup')
                    if body.get('environmentGroup') is not None
                    else purchase_tag_for_site(STATE.cfg, site))
                purchaser_label = body.get('buyer')

                def finish_backup_environment_batch():
                    try:
                        payload = STATE.backup_environment_result_payload(
                            operation_run_key, site, purchase_date,
                            environment_group, purchaser_label)
                        STATE.enqueue_operation_result(
                            '/v1/operations/environment-creation-runs',
                            'resource.environment.create', payload)
                    except Exception as exc:
                        if hasattr(STATE, 'operation_sync_error'):
                            STATE.operation_sync_error = scrub_text(exc)[:200]
                    finally:
                        STATE.tasks.finish(task_id)
                try:
                    count = STATE.backup_job.start(
                        body.get('buyer'), body.get('count'), body.get('type'),
                        purchase_date,
                        verify_sample_count=body.get('verifySampleCount', 1),
                        confirm_write=bool(body.get('confirmWrite')),
                        site=site,
                        environment_group=body.get('environmentGroup'),
                        reserve_resources=lambda resources:
                            STATE.tasks.reserve(task_id, resources),
                        on_finished=finish_backup_environment_batch)
                except Exception:
                    STATE.tasks.finish(task_id)
                    raise
                self._json({'started': True, 'count': count,
                            'taskId': task_id})
            elif path == '/api/envbatch/backup/stop':
                self._json(STATE.backup_job.request_stop(), 202)
            elif path == '/api/envbatch/preferences':
                lock = getattr(STATE, 'config_lock', None)
                with lock if lock is not None else nullcontext():
                    task_service = getattr(STATE, 'tasks', None)
                    task_snapshot = (
                        task_service.snapshot() if task_service is not None
                        else {'tasks': []})
                    if any(item.get('kind') in ('env_batch', 'backup_env')
                           for item in task_snapshot.get('tasks') or []):
                        raise RuntimeError(
                            '环境创建任务运行中，不能切换站点或采购分组')
                    old_cfg = load_config()
                    cfg = updated_envbatch_preferences(old_cfg, body)
                    cfg = save_config(cfg)
                    STATE.cfg = cfg
                self._json({
                    'saved': True,
                    **public_envbatch_preferences(cfg),
                })
            elif path == '/api/config':
                request_identity = STATE.auth.require(
                    'system.integration.manage', role='super_admin')
                lock = getattr(STATE, 'config_lock', None)
                with lock if lock is not None else nullcontext():
                    service = state_local_config_service()
                    submitted = dict(body)
                    expected_revision = submitted.pop(
                        'expectedRevision', None)
                    old_cfg = service.load()
                    cfg = updated_config(old_cfg, submitted)
                    task_snapshot = STATE.tasks.snapshot()
                    if any(item.get('kind') == 'config'
                           for item in task_snapshot.get('tasks') or []):
                        raise RuntimeError(
                            '云端配置请求正在处理，不能同时修改本机配置')
                    runtime_fields = {
                        'hubPort', 'concurrency', 'envCreateWorkers',
                        'safeParallelTasks', 'queryBrowserMode',
                    }
                    if (STATE.tasks.running()
                            and any(cfg.get(name) != old_cfg.get(name)
                                    for name in runtime_fields)):
                        raise RuntimeError(
                            '后台任务运行中，不能修改端口、并发数或并行模式')
                    committed = service.commit(
                        cfg,
                        expected_revision=expected_revision,
                        source='desktop_local_api')
                    cfg = committed['config']
                    STATE.cfg = cfg
                    reconnect_needed = (
                        cfg.get('hubPort') != old_cfg.get('hubPort')
                        or cfg.get('concurrency') != old_cfg.get('concurrency'))
                    connected = (STATE.reconnect_hub() if reconnect_needed
                                 else STATE.hub_status()[0])
                self._json({
                    'saved': True,
                    'hubConnected': connected,
                    'configRevision': committed['configRevision'],
                    'changedFields': committed['changedFields'],
                })
            elif path == DATA_SOURCE_API_PREFIX + '/claim-personal':
                member_id = request_identity['user']['id']
                include_all = bool(set(request_identity.get('roles') or []) & {
                    'admin', 'super_admin'})
                STATE.data_sources.claim_legacy_personal(
                    member_id,
                    body.get('sourceId'),
                    expected_revision=body.get('expectedRevision'))
                self._json({
                    'saved': True,
                    **STATE.data_sources.public_snapshot(
                        member_id, include_all=include_all),
                })
            elif path == DATA_SOURCE_API_PREFIX + '/metadata':
                editable_data_source(request_identity, body.get('sourceId'))
                STATE.data_sources.update_source_metadata(
                    body.get('sourceId'), body.get('label'),
                    body.get('enabled'),
                    expected_revision=body.get('expectedRevision'))
                member_id = request_identity['user']['id']
                include_all = bool(set(request_identity.get('roles') or []) & {
                    'admin', 'super_admin'})
                self._json({
                    'saved': True,
                    **STATE.data_sources.public_snapshot(
                        member_id, include_all=include_all),
                })
            elif path == DATA_SOURCE_API_PREFIX + '/replace':
                source = editable_data_source(
                    request_identity, body.get('sourceId'),
                    allow_unclaimed=True)
                STATE.data_sources.service.assert_revision(
                    body.get('expectedRevision'))
                member_id = request_identity['user']['id']
                target = STATE.purchase_assistant.consume_validated_target(
                    body.get('validationId'), owner_key=member_id)
                owner = (
                    source.get('ownerMemberId') or member_id
                    if source['scope'] == 'personal' else '')
                STATE.data_sources.replace_source_target(
                    source['id'], target, owner_member_id=owner,
                    expected_revision=body.get('expectedRevision'))
                include_all = bool(set(request_identity.get('roles') or []) & {
                    'admin', 'super_admin'})
                self._json({
                    'saved': True,
                    **STATE.data_sources.public_snapshot(
                        member_id, include_all=include_all),
                })
            elif path == DATA_SOURCE_API_PREFIX + '/revalidate':
                source = editable_data_source(
                    request_identity, body.get('sourceId'),
                    allow_unclaimed=True)
                self._json({
                    'ok': True,
                    **STATE.purchase_assistant.revalidate_target(source),
                })
            elif path == DATA_SOURCE_API_PREFIX + '/inspect':
                member_id = request_identity['user']['id']
                self._json({
                    'ok': True,
                    **STATE.purchase_assistant.inspect_source(
                        body.get('spreadsheetUrl'), owner_key=member_id),
                })
            elif path == DATA_SOURCE_API_PREFIX + '/validate':
                member_id = request_identity['user']['id']
                self._json({
                    'ok': True,
                    **STATE.purchase_assistant.validate_source(
                        body.get('inspectionId'), body.get('selectionId'),
                        owner_key=member_id),
                })
            elif path == DATA_SOURCE_API_PREFIX + '/personal':
                member_id = request_identity['user']['id']
                STATE.data_sources.service.assert_revision(
                    body.get('expectedRevision'))
                target = STATE.purchase_assistant.consume_validated_target(
                    body.get('validationId'), owner_key=member_id)
                STATE.data_sources.upsert_personal(
                    member_id, target,
                    expected_revision=body.get('expectedRevision'))
                include_all = bool(set(request_identity.get('roles') or []) & {
                    'admin', 'super_admin'})
                self._json({
                    'saved': True,
                    **STATE.data_sources.public_snapshot(
                        member_id, include_all=include_all),
                })
            elif path == DATA_SOURCE_API_PREFIX + '/team':
                if not set(request_identity.get('roles') or []) & {
                        'admin', 'super_admin'}:
                    raise LocalAuthError('permission_denied', status=403)
                member_id = request_identity['user']['id']
                STATE.data_sources.service.assert_revision(
                    body.get('expectedRevision'))
                target = STATE.purchase_assistant.consume_validated_target(
                    body.get('validationId'), owner_key=member_id)
                STATE.data_sources.upsert_team(
                    target,
                    set_default=bool(body.get('setDefault')),
                    expected_revision=body.get('expectedRevision'))
                self._json({
                    'saved': True,
                    **STATE.data_sources.public_snapshot(
                        member_id, include_all=True),
                })
            elif path == DATA_SOURCE_API_PREFIX + '/buyer-default':
                member_id = request_identity['user']['id']
                include_all = bool(set(request_identity.get('roles') or []) & {
                    'admin', 'super_admin'})
                STATE.data_sources.set_buyer_default(
                    member_id,
                    body.get('sourceId'),
                    expected_revision=body.get('expectedRevision'))
                self._json({
                    'saved': True,
                    **STATE.data_sources.public_snapshot(
                        member_id, include_all=include_all),
                })
            elif path == DATA_SOURCE_API_PREFIX + '/buyer-default/clear':
                member_id = request_identity['user']['id']
                include_all = bool(set(request_identity.get('roles') or []) & {
                    'admin', 'super_admin'})
                STATE.data_sources.use_team_default(
                    member_id,
                    expected_revision=body.get('expectedRevision'))
                self._json({
                    'saved': True,
                    **STATE.data_sources.public_snapshot(
                        member_id, include_all=include_all),
                })
            elif path == DATA_SOURCE_API_PREFIX + '/environment-binding':
                if not set(request_identity.get('roles') or []) & {
                        'admin', 'super_admin'}:
                    raise LocalAuthError('permission_denied', status=403)
                member_id = str(body.get('memberId') or '').strip()
                STATE.data_sources.bind_environment(
                    body.get('containerCode'),
                    member_id,
                    body.get('sourceId'),
                    expected_revision=body.get('expectedRevision'))
                self._json({
                    'saved': True,
                    **STATE.data_sources.public_snapshot(
                        request_identity['user']['id'], include_all=True),
                })
            elif path == (DATA_SOURCE_API_PREFIX
                          + '/environment-binding/remove'):
                if not set(request_identity.get('roles') or []) & {
                        'admin', 'super_admin'}:
                    raise LocalAuthError('permission_denied', status=403)
                STATE.data_sources.unbind_environment(
                    body.get('containerCode'), body.get('memberId'),
                    expected_revision=body.get('expectedRevision'))
                self._json({
                    'saved': True,
                    **STATE.data_sources.public_snapshot(
                        request_identity['user']['id'], include_all=True),
                })
            elif path == DATA_SOURCE_API_PREFIX + '/team-default':
                if not set(request_identity.get('roles') or []) & {
                        'admin', 'super_admin'}:
                    raise LocalAuthError('permission_denied', status=403)
                STATE.data_sources.set_team_default(
                    body.get('sourceId'),
                    expected_revision=body.get('expectedRevision'))
                self._json({
                    'saved': True,
                    **STATE.data_sources.public_snapshot(
                        request_identity['user']['id'], include_all=True),
                })
            elif path == DATA_SOURCE_API_PREFIX + '/team-default/clear':
                if not set(request_identity.get('roles') or []) & {
                        'admin', 'super_admin'}:
                    raise LocalAuthError('permission_denied', status=403)
                STATE.data_sources.clear_team_default(
                    expected_revision=body.get('expectedRevision'))
                self._json({
                    'saved': True,
                    **STATE.data_sources.public_snapshot(
                        request_identity['user']['id'], include_all=True),
                })
            elif path == '/api/lark/config':
                raise LocalAuthError(
                    'cloud_managed',
                    '企业应用凭证只能由超级管理员在云端统一配置',
                    status=410,
                )
            elif path == '/api/lark/target-metadata':
                cfg = refreshed_lark_target_labels(
                    STATE.cfg, STATE.lark_credentials)
                cfg = save_config(cfg)
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
                cfg = save_config(cfg)
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
        except LocalConfigRevisionConflict as e:
            self._json({
                'error': str(e),
                'code': e.code,
                'configRevision': e.actual_revision,
            }, 409)
        except DataSourceRegistryError as e:
            self._json({'error': str(e), 'code': e.code}, 409)
        except PurchaseAssistantError as e:
                self._json({
                    'error': str(e),
                    'code': 'source_invalid',
                }, 422)
        except HubCoreRepairError as e:
            self._json({'error': str(e), 'code': e.code}, e.status)
        except HubApiError as e:
            self._json({
                'error': str(e),
                'code': e.reason_code,
            }, 503)
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


def browser_launch_url(local_url, argv=None, auth_service=None):
    """Choose the page opened by the desktop launcher.

    The local HTTP service continues to run for HubStudio/CDP/SHEIN work, but
    the employee-facing default is the cloud workspace.  ``--local-ui`` is an
    explicit compatibility and troubleshooting entry point.
    """
    if '--local-ui' in (argv or ()):  # Keep the local UI opt-in explicit.
        return str(local_url)
    client = getattr(auth_service, 'client', None)
    cloud_url = str(
        getattr(client, 'base_url', '') or DEFAULT_AUTH_BASE_URL
    ).strip().rstrip('/')
    return cloud_url or DEFAULT_AUTH_BASE_URL


def main(argv=None):
    global STATE
    argv = argv or sys.argv[1:]
    instance_guard = acquire_executor_instance_guard()
    if not instance_guard.acquired:
        print('Xynigo 本地执行器已经在运行，本次启动不再创建第二个实例。')
        return
    port = load_config()['serverPort']
    no_browser = '--no-browser' in argv
    try:
        STATE = build_state()
    except Exception:
        instance_guard.close()
        raise
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
    local_url = 'http://127.0.0.1:%s' % port
    executor_rpc_token = secrets.token_urlsafe(48)
    server.executor_rpc_token = executor_rpc_token
    workspace_rpc = WorkspaceRpcClient(local_url, executor_rpc_token)
    STATE.executor_channel.workspace_rpc_executor = workspace_rpc.execute
    STATE.executor_channel.operation_task_executor = LocalOperationExecutor(
        workspace_rpc.execute).execute
    launch_url = browser_launch_url(local_url, argv, STATE.auth)
    print('Xynigo Sourcing v%s  本地执行器运行中：%s' % (
        __version__, local_url))
    print('浏览器工作台：%s' % launch_url)
    if launch_url != local_url:
        print('需要本机界面时使用 --local-ui 启动。')
    print('保持此窗口开启；关闭窗口即退出工具。')
    ok, err = STATE.hub_status(force=True)
    if ok:
        print('HubStudio 连接：正常')
    else:
        print('HubStudio 连接：失败 —— %s' % err)
    STATE.updates.check_async()
    STATE.executor_channel.start()
    if not no_browser:
        def _open():
            ok = False
            try:
                ok = webbrowser.open(launch_url)
            except Exception:
                pass
            if not ok and hasattr(os, 'startfile'):   # Windows 兜底
                try:
                    os.startfile(launch_url)
                    ok = True
                except Exception:
                    pass
            if not ok:
                print('浏览器未能自动打开，请手动访问：%s' % launch_url)
        threading.Timer(0.6, _open).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        STATE.executor_channel.stop()
        server.server_close()
        instance_guard.close()
