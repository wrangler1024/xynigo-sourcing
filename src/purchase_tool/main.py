# -*- coding: utf-8 -*-
"""采购工具服务入口：标准库 HTTP 服务 + 自动打开浏览器。

启动：python -m purchase_tool   （或打包后的 exe / 启动脚本）
API：
  GET  /                    操作页面
  GET  /api/hub-status      探测 HubStudio 是否在线
  GET  /api/groups          分组列表
  GET  /api/group-envs      指定分组的环境序号（查全部分组用）
  POST /api/query           {serials:[...]} 或 {group:"分组名"}
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
import secrets
import shutil
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from . import __version__
from .buyer_register import BuyerRegistrationTask, RegistrationOrchestrator
from .excel_export import EXPORT_HEAD, export_bytes
from .env_batch import (TAGS, BatchEnvOrchestrator, ResumeStateStore,
                        batch_fingerprint, build_batch_plan,
                        mapping_workbook_bytes, parse_assignment,
                        parse_vendor_workbook)
from .hub_api import HubStudioApi, DEFAULT_PORT
from .lark_ledger import LarkLedgerSink
from .shein_query import QueryOrchestrator
from .updater import UpdateCoordinator

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
INDEX_HTML = os.path.join(BASE_DIR, 'web', 'index.html')
LOGO_PNG = os.path.join(BASE_DIR, 'web', 'xynigo-logo.png')
MASCOT_X_PNG = os.path.join(BASE_DIR, 'web', 'xynigo-mascot-x.png')
X_ICON_PNG = os.path.join(BASE_DIR, 'web', 'xynigo-x.png')
X_ICON_ICO = os.path.join(BASE_DIR, 'web', 'xynigo-x.ico')
FAVICON_PNG = os.path.join(BASE_DIR, 'web', 'xynigo-favicon.png')
FAVICON_ICO = os.path.join(BASE_DIR, 'web', 'xynigo-favicon.ico')
ENV_TEMPLATE_XLSX = os.path.join(
    BASE_DIR, 'web', '采购工具买家号入库模板.xlsx')
CONFIG_PATH = os.path.join(os.getcwd(), 'config.json')
LOG_DIR = os.path.join(os.getcwd(), '查询日志')

def load_config():
    cfg = {
        'hubPort': DEFAULT_PORT,
        'serverPort': 8765,
        'concurrency': 2,
        'importBuyerPlan': '1:Operator-A',
        'verifySampleCount': 3,
        'hiddenQueryColumns': ['envName', 'ip'],
    }
    try:
        with open(CONFIG_PATH, encoding='utf-8') as f:
            cfg.update(json.load(f))
    except Exception:
        pass
    return cfg


def save_config(cfg):
    with open(CONFIG_PATH, 'w', encoding='utf-8') as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


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
        self.env_job = EnvBatchJob(lambda: self.hub)
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
                 'env': t.env_serial or t.env_name} for t in tasks]

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


def ledger_tsv_bytes(rows):
    """生成显式下载用台账 TSV；返回值含凭证，禁止日志输出。"""
    output = StringIO(newline='')
    writer = csv.writer(output, dialect='excel-tab', lineterminator='\r\n')
    writer.writerow([
        '邮箱账号', '密码', '接码Key链接', '号商购买单号', '账号状态',
        '采购员', 'Cookie', '备注', '绑定环境', '环境序号', '绑定时间'])
    for row in rows:
        complete = row.state == 'done'
        writer.writerow([
            row.account.email,
            row.account.password,
            row.account.key_url,
            row.account.order_no,
            '已绑定' if complete else '未绑定',
            row.account.buyer,
            row.account.cookie_text,
            '模块三 TSV 应急直贴',
            row.env_name if complete else '',
            row.serial_number if complete else '',
            row.binding_time if complete else '',
        ])
    return ('\ufeff' + output.getvalue()).encode('utf-8')


class EnvBatchJob(object):
    """模块三后台任务：凭证仅保存在短生命周期内存对象。"""

    MAX_UPLOAD_BYTES = 20 * 1024 * 1024
    PENDING_TTL_SECONDS = 30 * 60
    RESULT_CREDENTIAL_TTL_SECONDS = 15 * 60

    def __init__(self, hub_getter):
        self.hub_getter = hub_getter
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

    def preview(self, plan_id, assignment, purchase_date):
        with self.lock:
            self._clean_pending()
            pending = self.pending.get(plan_id)
        if not pending:
            raise ValueError('解析计划已过期，请重新选择 xlsx')
        parse_assignment(assignment, len(pending['accounts']))
        existing = self.hub_getter().env_list(TAGS['MX'])
        plan = build_batch_plan(
            pending['accounts'], assignment, existing_envs=existing,
            purchase_date=purchase_date)
        return [{
            'emailMasked': row.account.safe_email,
            'buyer': row.account.buyer,
            'envName': row.env_name,
            'recoveredExisting': row.recovered_existing,
        } for row in plan]

    @staticmethod
    def _safe_error(exc, accounts):
        text = str(exc)
        for account in accounts:
            for value in (account.email, account.password,
                          account.key_url, account.cookie_text):
                if value:
                    text = text.replace(value, '<redacted>')
        from .redaction import scrub_text
        return scrub_text(text)[:300]

    def _set_rows(self, rows):
        with self.lock:
            self.rows = [dict(row) for row in rows]

    def _clear_sensitive(self):
        with self.lock:
            self.tsv_data = None
            if self.runner:
                for row in self.runner.rows:
                    row.account.password = ''
                    row.account.key_url = ''
                    row.account.cookie_text = ''

    def _schedule_sensitive_cleanup(self):
        if self._sensitive_timer:
            self._sensitive_timer.cancel()
        self._sensitive_timer = threading.Timer(
            self.RESULT_CREDENTIAL_TTL_SECONDS, self._clear_sensitive)
        self._sensitive_timer.daemon = True
        self._sensitive_timer.start()

    def start(self, plan_id, assignment, purchase_date,
              verify_sample_count=3, confirm_write=False):
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
            pending = self.pending.pop(plan_id, None)
            if not pending:
                raise ValueError('解析计划已过期，请重新选择 xlsx')
            parse_assignment(assignment, len(pending['accounts']))
            self.running = True
            self.started_at = time.time()
            self.finished_at = None
            self.rows = []
            self.summary = {}
            self.ip_checks = []
            self.fatal_error = ''
            self.mapping_data = None
            self.tsv_data = None
        source = pending['source']
        accounts = pending['accounts']
        batch_id = batch_fingerprint(source, assignment, 'MX', purchase_date)
        pending['source'] = b''

        def worker():
            runner = BatchEnvOrchestrator(
                self.hub_getter(), site='MX', purchase_date=purchase_date,
                state_store=ResumeStateStore(batch_id),
                on_progress=self._set_rows)
            self.runner = runner
            try:
                runner.prepare(accounts, assignment)
                result_rows = runner.run()
                mapping = mapping_workbook_bytes(result_rows)
                checks = runner.verify_ips(verify_sample_count)
                tsv = ledger_tsv_bytes(result_rows)
                done = sum(row.state == 'done' for row in result_rows)
                with self.lock:
                    self.mapping_data = mapping
                    self.mapping_name = '绑定映射清单_%s.xlsx' % purchase_date
                    self.tsv_data = tsv
                    self.tsv_name = '台账直贴_%s.tsv' % purchase_date
                    self.ip_checks = checks
                    self.summary = {
                        'total': len(result_rows),
                        'done': done,
                        'failed': len(result_rows) - done,
                        'ipOk': sum(bool(item.get('ok')) for item in checks),
                        'ipTotal': len(checks),
                    }
                self._schedule_sensitive_cleanup()
            except Exception as exc:
                with self.lock:
                    self.fatal_error = self._safe_error(exc, accounts)
            finally:
                if self.fatal_error:
                    self._clear_sensitive()
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
            self.running = True
            self.started_at = time.time()
            self.finished_at = None

        def worker():
            try:
                self.runner.retry_one(account_id)
                result_rows = self.runner.rows
                with self.lock:
                    self.mapping_data = mapping_workbook_bytes(result_rows)
                    self.tsv_data = ledger_tsv_bytes(result_rows)
                    self.summary.update({
                        'total': len(result_rows),
                        'done': sum(row.state == 'done' for row in result_rows),
                        'failed': sum(row.state != 'done' for row in result_rows),
                    })
                self._schedule_sensitive_cleanup()
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
            elif path == '/xynigo-favicon.png':
                self._file(FAVICON_PNG, 'image/png')
            elif path == '/favicon.ico':
                self._file(FAVICON_ICO, 'image/x-icon')
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
                self._json(STATE.cfg)
            else:
                self._json({'error': 'not found'}, 404)
        except ConnectionError as e:
            self._json({'error': 'HubStudio 未连接：%s' % e}, 503)
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
                if STATE.env_job.running:
                    return self._json({'error': '模块三建环境正在进行'}, 409)
                serials = body.get('serials')
                group = body.get('group')
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
                STATE.orch.start_batch(serials, env_index)
                self._json({'started': True, 'total': len(serials)})
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
                if STATE.env_job.running:
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
                    body.get('purchaseDate') or time.strftime('%Y%m%d'))
                self._json({'valid': True, 'count': len(rows), 'rows': rows})
            elif path == '/api/envbatch/start':
                if STATE.orch.running or STATE.reg_job.running:
                    return self._json({
                        'error': '模块一/二任务正在进行，请结束后再建环境'}, 409)
                count = STATE.env_job.start(
                    body.get('planId'), body.get('assignment'),
                    body.get('purchaseDate') or time.strftime('%Y%m%d'),
                    verify_sample_count=body.get('verifySampleCount', 3),
                    confirm_write=bool(body.get('confirmWrite')))
                self._json({'started': True, 'count': count})
            elif path == '/api/envbatch/retry-row':
                STATE.env_job.retry_row(str(body.get('accountId') or ''))
                self._json({'started': True})
            elif path == '/api/config':
                old_cfg = load_config()
                cfg = dict(old_cfg)
                cfg.update(body)
                try:
                    cfg['concurrency'] = max(
                        1, min(5, int(cfg.get('concurrency', 2))))
                except (TypeError, ValueError):
                    cfg['concurrency'] = 2
                try:
                    cfg['verifySampleCount'] = max(
                        0, min(10, int(cfg.get('verifySampleCount', 3))))
                except (TypeError, ValueError):
                    cfg['verifySampleCount'] = 3
                cfg['importBuyerPlan'] = str(
                    cfg.get('importBuyerPlan') or '1:Operator-A')[:200]
                hidden = cfg.get('hiddenQueryColumns') or []
                if not isinstance(hidden, list):
                    hidden = []
                cfg['hiddenQueryColumns'] = [
                    name for name in ('envName', 'ip') if name in hidden]
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
