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
import shutil
import sys
import tempfile
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from . import __version__
from .buyer_register import BuyerRegistrationTask, RegistrationOrchestrator
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
from .hub_api import HubStudioApi, DEFAULT_PORT
from .lark_ledger import LarkLedgerSink
from .shein_query import QueryOrchestrator, normalize_site
from .updater import UpdateCoordinator

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
INDEX_HTML = os.path.join(BASE_DIR, 'web', 'index.html')
LOGO_PNG = os.path.join(BASE_DIR, 'web', 'xynigo-logo.png')
MASCOT_X_PNG = os.path.join(BASE_DIR, 'web', 'xynigo-mascot-x.png')
X_ICON_PNG = os.path.join(BASE_DIR, 'web', 'xynigo-x.png')
X_ICON_ICO = os.path.join(BASE_DIR, 'web', 'xynigo-x.ico')
ENV_TEMPLATE_XLSX = os.path.join(
    BASE_DIR, 'web', '采购工具买家号入库模板.xlsx')
CONFIG_PATH = os.path.join(os.getcwd(), 'config.json')
LOG_DIR = os.path.join(os.getcwd(), '查询日志')

CONFIG_FIELDS = frozenset({
    'hubPort', 'serverPort', 'concurrency', 'importBuyerPlan',
    'verifySampleCount', 'hiddenQueryColumns', 'purchaseSite',
    'purchaseTag', 'purchaseTags', 'proxyLink', 'envCreateWorkers',
})
CONFIG_REQUEST_FIELDS = CONFIG_FIELDS | {'proxyClear'}


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
              if key in CONFIG_FIELDS and key != 'proxyLink'}
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
            'proxyLink', 'purchaseSite', 'purchaseTag', 'purchaseTags'}:
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

    def __init__(self):
        cfg = load_config()
        self.cfg = cfg
        self.hub = HubStudioApi(port=cfg['hubPort'])
        self._hub_status = HubStatusCache(lambda: self.hub)
        self.orch = QueryOrchestrator(
            self.hub, log_dir=LOG_DIR,
            concurrency=cfg.get('concurrency', 2))
        self.reg_job = RegistrationJob(lambda: self.hub)
        self.env_job = EnvBatchJob(lambda: self.hub, lambda: self.cfg)
        self.backup_job = BackupEnvJob(lambda: self.hub, lambda: self.cfg)
        self.updates = UpdateCoordinator(
            os.environ.get('XYNIGO_INSTALL_DIR'), __version__)

    def reconnect_hub(self):
        self.orch.close()
        self.hub = HubStudioApi(port=self.cfg['hubPort'])
        self.orch = QueryOrchestrator(
            self.hub, log_dir=LOG_DIR,
            concurrency=self.cfg.get('concurrency', 2))
        self._hub_status.reset()
        return self.hub_status(force=True)[0]

    def hub_status(self, force=False):
        return self._hub_status.check(force=force)


def build_state():
    return AppState()


STATE = None


class RegistrationJob(object):
    """注册模块后台任务：只保留脱敏进度，不保存原始凭证。"""

    def __init__(self, hub_getter):
        self.hub_getter = hub_getter
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
              write_lark_ledger=False):
        if self.running:
            raise RuntimeError('已有注册任务在进行')
        if not accept_terms:
            raise ValueError('真实注册必须确认 SHEIN 条款')
        tasks = self.parse_tasks(raw_tasks)
        if write_lark_ledger and any(not t.record_id for t in tasks):
            raise ValueError(
                '勾选台账回写时，每个任务都必须提供 record_id')
        if write_lark_ledger and not shutil.which('lark-cli'):
            raise ValueError('本机未安装 lark-cli，不能勾选台账回写')
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
                ledger_sink=LarkLedgerSink() if write_lark_ledger else None,
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


LEDGER_PASTE_COLUMNS = {
    # 飞书「希音采购买家号台账」默认 Grid View 的连续列契约。
    # 直贴文件从「邮箱账号」列开始，不包含左侧自动编号「账号ID」。
    # 「购买日期」是公式字段，必须保留一个空占位，后续字段才不会错位。
    'MX': (
        '邮箱账号', '密码', '接码Key链接', 'Cookie', '号商购买单号',
        '购买日期', '账号状态', '绑定环境', '环境序号', '采购员', '绑定时间'),
    'US': (
        '邮箱账号', '密码', 'Cookie', '接码Key链接', '号商购买单号',
        '购买日期', '账号状态', '绑定环境', '环境序号', '绑定时间', '采购员'),
}


def ledger_tsv_bytes(rows, site):
    """生成按站点对齐飞书视图的无表头直贴 TSV（含凭证）。"""
    site = normalize_env_site(site)
    output = StringIO(newline='')
    writer = csv.writer(output, dialect='excel-tab', lineterminator='\r\n')
    for row in rows:
        complete = row.state == 'done'
        values = {
            '邮箱账号': row.account.email,
            '密码': row.account.password,
            '接码Key链接': row.account.key_url,
            'Cookie': row.account.cookie_text,
            '号商购买单号': row.account.order_no,
            '购买日期': '',
            '账号状态': '已绑定' if complete else '未绑定',
            '绑定环境': row.env_name if complete else '',
            '环境序号': row.serial_number if complete else '',
            '采购员': row.account.buyer,
            '绑定时间': row.binding_time if complete else '',
        }
        writer.writerow([values[name] for name in LEDGER_PASTE_COLUMNS[site]])
    return ('\ufeff' + output.getvalue()).encode('utf-8')


def ledger_tsv_filename(site, purchase_date):
    site = normalize_env_site(site)
    return ('台账直贴_%s_%s_无表头_从邮箱账号列开始.tsv' %
            (site, purchase_date))


class EnvBatchJob(object):
    """模块三后台任务：凭证仅保存在短生命周期内存对象。"""

    MAX_UPLOAD_BYTES = 20 * 1024 * 1024
    PENDING_TTL_SECONDS = 30 * 60
    RESULT_CREDENTIAL_TTL_SECONDS = 15 * 60

    def __init__(self, hub_getter, config_getter=load_config):
        self.hub_getter = hub_getter
        self.config_getter = config_getter
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

    def start(self, plan_id, assignment, purchase_date,
              verify_sample_count=3, confirm_write=False, site='MX'):
        if not confirm_write:
            raise ValueError('正式执行必须二次确认 HubStudio 写入')
        try:
            verify_sample_count = max(0, min(10, int(verify_sample_count)))
        except (TypeError, ValueError) as exc:
            raise ValueError('出口 IP 抽查数必须是 0-10 的整数') from exc
        with self.lock:
            if self.running:
                raise RuntimeError('已有模块三任务在进行')
            self._clean_pending()
            pending = self.pending.get(plan_id)
            if not pending:
                raise ValueError('解析计划已过期，请重新选择 xlsx')
            account_count = len(pending['accounts'])
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
        build_batch_plan(
            pending['accounts'], assignment,
            existing_envs=selected_existing,
            site=runtime['site'], purchase_date=purchase_date,
            all_existing_envs=all_existing)
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
                tsv = ledger_tsv_bytes(result_rows, runtime['site'])
                done = sum(row.state == 'done' for row in result_rows)
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
            if not row.account.password or not row.account.cookie_text:
                raise ValueError('凭证内存已清理，请重新选择原始 xlsx 后续跑')
            self._cancel_sensitive_cleanup_locked()
            self.running = True
            self.started_at = time.time()
            self.finished_at = None

        def worker():
            try:
                self.runner.retry_one(account_id)
                result_rows = self.runner.rows
                with self.lock:
                    self.mapping_data = mapping_workbook_bytes(result_rows)
                    self.tsv_data = ledger_tsv_bytes(
                        result_rows, self.runner.site)
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
                'mappingReady': self.mapping_data is not None,
                'tsvReady': self.tsv_data is not None,
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
            verify_sample_count = max(0, min(10, int(verify_sample_count)))
        except (TypeError, ValueError) as exc:
            raise ValueError('出口 IP 抽查数必须是 0-10 的整数') from exc
        buyer, count, backup_type, purchase_date = self._validate_params(
            buyer, count, backup_type, purchase_date)
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

    def _json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        length = int(self.headers.get('Content-Length') or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode('utf-8'))
        except Exception:
            return {}

    def _file(self, path, mime):
        with open(path, 'rb') as f:
            body = f.read()
        self.send_response(200)
        content_type = (mime + '; charset=utf-8'
                        if mime.startswith('text/') else mime)
        self.send_header('Content-Type', content_type)
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

    # ---- GET ----

    def do_GET(self):
        parsed = urlparse(self.path)
        path, query = parsed.path, parse_qs(parsed.query)
        try:
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
            else:
                self._json({'error': 'not found'}, 404)
        except ConnectionError as e:
            self._json({'error': 'HubStudio 未连接：%s' % e}, 503)
        except ValueError as e:
            self._json({'error': str(e)}, 400)
        except Exception as e:
            self._json({'error': str(e)}, 500)

    # ---- POST ----

    def do_POST(self):
        path = urlparse(self.path).path
        body = self._body()
        try:
            if path == '/api/query':
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
                    write_lark_ledger=bool(body.get('writeLarkLedger')))
                self._json({'started': True, 'count': count})
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
                    site=body.get('site') or 'MX')
                self._json({'started': True, 'count': count})
            elif path == '/api/envbatch/retry-row':
                STATE.env_job.retry_row(str(body.get('accountId') or ''))
                self._json({'started': True})
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
            else:
                self._json({'error': 'not found'}, 404)
        except RuntimeError as e:
            self._json({'error': str(e)}, 409)
        except ValueError as e:
            self._json({'error': str(e)}, 400)
        except ConnectionError as e:
            self._json({'error': 'HubStudio 未连接：%s' % e}, 503)
        except Exception as e:
            self._json({'error': str(e)}, 500)


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
