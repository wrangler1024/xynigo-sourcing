# -*- coding: utf-8 -*-
"""SHEIN 墨西哥/美国站页面解析 + 逐环境查询编排。

解析正则全部移植自已实战验证的 .mjs 脚本（2026-08-17 首批 10 单跑通）：
- check_order_status.mjs：订单列表页取订单号/金额/状态/阶段
- check_tracking.mjs：详情页 SSR 数据取物流单号/包裹号/承运商/砍单标记

关键事实（04-踩坑速查表）：
- 物流单号在详情页 SSR <script> JSON 的 shipping_no 字段，innerText 取不到，
  必须对整页 HTML 正则
- 一单可拆多包裹：goods_pkg_rel_list 是数组，逐个取
- 状态须取订单卡片区域（"Detalles de Pedido" 之前），否则会误读顶部标签栏
- 详情页 10 位数字是收件人电话，不是单号（正则锚定 "shipping_no":" 前缀可避开）
- 砍单 = Reembolsando / reembolso está siendo procesado（列表状态与详情文案
  双信号，任一命中即判定）
"""
import os
import queue as queue_mod
import re
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone

from .cdp import CdpClient
from .hub_api import HubApiError

SUPPORTED_SITES = ('MX', 'US')
SITE_LABELS = {'MX': '墨西哥站', 'US': '美国站'}
SYSTEMIC_HUB_FAILURE_CODES = {
    'hubstudio_client_not_running',
    'hubstudio_local_api_unreachable',
    'hubstudio_local_api_timeout',
    'hubstudio_local_api_disabled',
    'hubstudio_local_api_authentication_required',
    'hubstudio_local_api_authentication_failed',
    'hubstudio_local_api_incompatible',
    'hubstudio_browser_core_missing',
    'hubstudio_browser_launch_invalid',
}
SITE_PROFILES = {
    'MX': {
        'baseUrl': 'https://www.shein.com.mx',
        'utcOffsetMinutes': -6 * 60,
    },
    'US': {
        'baseUrl': 'https://us.shein.com',
        # 2026-08-19 实测美国采购环境为 America/Chicago（夏令时 UTC-5）。
        # 查询时会优先读取环境浏览器的真实时区偏移，这里仅作降级值。
        'utcOffsetMinutes': -5 * 60,
    },
}

# 状态取值（西班牙语 / 英语）→ 中文
STATUS_CN = {
    'Procesando': '备货中', 'Empacando': '打包中', 'Enviado': '已发货',
    'Entregado': '已送达', 'Completado': '已完成', 'Cancelado': '已取消',
    'Reembolsando': '砍单退款中', 'Devolución': '退换货', 'No pagado': '未支付',
    'Processing': '备货中', 'Packing': '打包中', 'Shipped': '已发货',
    'Delivered': '已送达', 'Completed': '已完成', 'Canceled': '已取消',
    'Cancelled': '已取消', 'Refunding': '退款中', 'Refunded': '已退款',
    'Returned': '已退货', 'Unpaid': '未支付', 'Paid': '已支付/待备货',
    'Risk verification': '风险订单/待验证',
}

RE_ORDER_NO_BY_SITE = {
    'MX': re.compile(r'Núm\.?\s*de\s*pedido\s*([A-Z0-9]+)', re.I),
    'US': re.compile(r'Order\s+NO\.?\s*([A-Z0-9]+)', re.I),
}
# 下单时间在列表页订单号文案之前：
# MX: "17 Ago 2026 03:14:31Núm. de pedido XXX"
# US: "Aug 17 2026 06:29:46Order NO. XXX"
RE_ORDER_TIME_BY_SITE = {
    'MX': re.compile(
        r'(\d{1,2}\s+[A-Za-z]{3,}\s+\d{4}\s+'
        r'\d{2}:\d{2}(?::\d{2})?)\s*Núm', re.I),
    'US': re.compile(
        r'([A-Za-z]{3,}\s+\d{1,2},?\s+\d{4}\s+'
        r'\d{2}:\d{2}(?::\d{2})?)\s*Order\s+NO', re.I),
}
MONTHS = {
    'MX': {'ene': '01', 'feb': '02', 'mar': '03', 'abr': '04',
           'may': '05', 'jun': '06', 'jul': '07', 'ago': '08',
           'sep': '09', 'oct': '10', 'nov': '11', 'dic': '12'},
    'US': {'jan': '01', 'feb': '02', 'mar': '03', 'apr': '04',
           'may': '05', 'jun': '06', 'jul': '07', 'aug': '08',
           'sep': '09', 'oct': '10', 'nov': '11', 'dec': '12'},
}
RE_AMOUNT_BY_SITE = {
    'MX': re.compile(r'\$MXN[\d.,]+'),
    'US': re.compile(r'(?<![A-Za-z])(?:US)?\$\s*[\d.,]+'),
}
RE_STAGE_BY_SITE = {
    'MX': re.compile(
        r'Almac[eé]n[^.]*\.|En tr[áa]nsito[^.]*\.|Preparaci[óo]n[^.]*\.'),
    'US': re.compile(
        r'International Warehouse[^.\n]*\.?|In transit[^.\n]*\.?'
        r'|Preparing[^.\n]*\.?|Loading complete', re.I),
}
RE_DETAIL_MARKER_BY_SITE = {
    'MX': re.compile(r'Detalles de Pedido', re.I),
    'US': re.compile(r'Order details', re.I),
}
RE_SHIPPING_NO = re.compile(r'"shipping_no":"([^"]+)"')
RE_PACKAGE_NO = re.compile(r'"package_no":"([^"]+)"')
RE_CARRIER = re.compile(r'"shipping_method_real":"([^"]{2,40})"')
RE_CARRIER_NAME = re.compile(r'"carrier_name":"([^"]{2,80})"')
RE_KANDAN_TEXT_BY_SITE = {
    'MX': re.compile(
        r'Reembolsando|reembolso est[áa] siendo procesado', re.I),
    # 避免把美国详情页固定导航项 "Refund" 误判成砍单。
    'US': re.compile(
        r'\bRefunding\b|\bRefunded\b|refund (?:is )?being processed'
        r'|refund in progress', re.I),
}
# 大小写不敏感：页面出现过 EMPACANDO 大写形式（2026-08-18 实测 1007）
RE_STATUS_WORDS_BY_SITE = {
    'MX': re.compile(
        r'(Procesando|Empacando|Enviado|Reembolsando|Completado|Cancelado'
        r'|Entregado|Devoluci[óo]n|No pagado)', re.I),
    'US': re.compile(
        r'(Processing|Packing|Shipped|Delivered|Completed|Canceled|Cancelled'
        r'|Refunding|Refunded|Returned|Unpaid|Paid)', re.I),
}
# 部分订单的列表卡片不渲染下单时间，但详情页 SSR 会保留 addTime。
# 2026-08-19 用 1001 对照列表时间验证：addTime 为订单创建时间（秒），
# paymentTime 为支付完成时间（毫秒），仅在 addTime 缺失时降级使用。
RE_DETAIL_ADD_TIME = re.compile(r'"addTime"\s*:\s*"?(\d{10,13})"?')
RE_DETAIL_PAYMENT_TIME = re.compile(
    r'"paymentTime"\s*:\s*"?(\d{10,13})"?')
RE_RISK_VERIFY_TEXT = re.compile(
    r'order is detected to be at risk and needs to be verified'
    r'|provide the supporting documents according to the instructions', re.I)
RE_RISK_VERIFY_FLAG = re.compile(r'"is_verify"\s*:\s*"?1"?', re.I)
RE_RISK_NOT_SUBMITTED = re.compile(
    r'"sensitive_status"\s*:\s*"no_submit"', re.I)
# Chrome 网络错误页（如 ERR_CONNECTION_CLOSED，711 代理偶发波动）
RE_ERR_PAGE = re.compile(r'(ERR_[A-Z_]+)')
# Cloudflare 安全校验页（2026-08-18 实测 1007：验证会自动通过但需额外等待）
RE_CF = re.compile(
    r'Verificaci[óo]n de seguridad|Checking your browser'
    r'|Attention Required|Verifica que (eres|no eres)', re.I)


def normalize_site(site):
    value = str(site or 'MX').strip().upper()
    if value not in SUPPORTED_SITES:
        raise ValueError('查询站点仅支持 MX（墨西哥）或 US（美国）')
    return value


def _site_profile(site):
    site = normalize_site(site)
    base = SITE_PROFILES[site]['baseUrl']
    return {
        'site': site,
        'label': SITE_LABELS[site],
        'ordersListUrl': base + '/user/orders/list',
        'orderDetailUrl': base + '/user/orders/detail/%s',
        'orderTrackUrl': base + '/orders/track?billno=%s',
        'utcOffsetMinutes': SITE_PROFILES[site]['utcOffsetMinutes'],
    }


def _query_timestamp(site='MX', utc_offset_minutes=None):
    """返回环境当地时间；浏览器偏移优先，站点配置仅作降级。"""
    site = normalize_site(site)
    if utc_offset_minutes is None:
        utc_offset_minutes = SITE_PROFILES[site]['utcOffsetMinutes']
    tz = timezone(timedelta(minutes=int(utc_offset_minutes)))
    return datetime.now(tz).strftime('%Y-%m-%d %H:%M:%S')


def _uniq(items):
    seen, out = set(), []
    for x in items:
        if x and x not in seen:
            seen.add(x)
            out.append(x)
    return out


def _norm_order_time(raw, site='MX'):
    """将 MX/US 页面月份文案统一为 ``YYYY-MM-DD HH:MM:SS``。"""
    site = normalize_site(site)
    if site == 'MX':
        m = re.match(r'(\d{1,2})\s+([A-Za-z]{3,})\s+(\d{4})\s+'
                     r'(\d{2}:\d{2}(?::\d{2})?)', raw or '')
        if not m:
            return raw or ''
        day, mon, year, hm = m.groups()
    else:
        m = re.match(r'([A-Za-z]{3,})\s+(\d{1,2}),?\s+(\d{4})\s+'
                     r'(\d{2}:\d{2}(?::\d{2})?)', raw or '')
        if not m:
            return raw or ''
        mon, day, year, hm = m.groups()
    mm = MONTHS[site].get(mon[:3].lower())
    return '%s-%s-%02d %s' % (year, mm, int(day), hm) if mm else raw


def _order_time_from_epoch(raw, site='MX', utc_offset_minutes=None):
    """SHEIN SSR Unix 时间戳 → 当前站点/环境的显示时间。"""
    try:
        site = normalize_site(site)
        stamp = int(raw)
        if stamp >= 10 ** 12:
            stamp /= 1000.0
        if utc_offset_minutes is None:
            utc_offset_minutes = SITE_PROFILES[site]['utcOffsetMinutes']
        tz = timezone(timedelta(minutes=int(utc_offset_minutes)))
        return datetime.fromtimestamp(
            stamp, timezone.utc).astimezone(tz).strftime(
                '%Y-%m-%d %H:%M:%S')
    except (TypeError, ValueError, OverflowError):
        return ''


def _canonical_status(word):
    wanted = (word or '').casefold()
    for canonical in STATUS_CN:
        if canonical.casefold() == wanted:
            return canonical
    return word or ''


def parse_list_page(text, site='MX'):
    """订单列表页解析。返回 {orderNo, orderTime, amount, status, statusCn, stage}。"""
    site = normalize_site(site)
    out = {'orderNo': None, 'orderTime': '', 'amount': None, 'status': None,
           'statusCn': None, 'stage': None}
    order_match = RE_ORDER_NO_BY_SITE[site].search(text)
    if order_match:
        out['orderNo'] = order_match.group(1)
    m = RE_ORDER_TIME_BY_SITE[site].search(text)
    if m:
        out['orderTime'] = _norm_order_time(m.group(1), site)
    if out['orderNo']:
        # 状态只取“当前订单号之后、详情按钮之前”的订单卡片区域。
        # 不能要求状态是区域最后一个词：1001 实测状态后还有仓库/包裹文案。
        detail_match = RE_DETAIL_MARKER_BY_SITE[site].search(
            text, order_match.end())
        card_end = detail_match.start() if detail_match else len(text)
        card = text[order_match.end():card_end]
        m = RE_AMOUNT_BY_SITE[site].search(card)
        if m:
            out['amount'] = m.group(0)
        m = RE_STATUS_WORDS_BY_SITE[site].search(card)
        if m:
            word = _canonical_status(m.group(1))
            out['status'] = word
            out['statusCn'] = STATUS_CN.get(word, '')
        m = RE_STAGE_BY_SITE[site].search(card)
        if m:
            out['stage'] = m.group(0)
    return out


def parse_detail_page(html, text, site='MX', utc_offset_minutes=None):
    """订单详情页解析。列表缺时间时可用 SSR addTime 回填。"""
    site = normalize_site(site)
    risk_order = bool(
        site == 'US' and (
            RE_RISK_VERIFY_TEXT.search(text or '') or
            (RE_RISK_VERIFY_FLAG.search(html or '') and
             RE_RISK_NOT_SUBMITTED.search(html or ''))))
    m = RE_CARRIER.search(html)
    carrier = m.group(1) if m else ''
    m = RE_DETAIL_ADD_TIME.search(html)
    if not m:
        m = RE_DETAIL_PAYMENT_TIME.search(html)
    return {
        'tracks': _uniq(RE_SHIPPING_NO.findall(html)),
        'pkgs': _uniq(RE_PACKAGE_NO.findall(html)),
        'carrier': friendly_carrier(carrier,
                                    _uniq(RE_SHIPPING_NO.findall(html))),
        'kanDan': bool(RE_KANDAN_TEXT_BY_SITE[site].search(text)),
        'riskOrder': risk_order,
        'riskMessage': ('订单被检测为风险订单，需尽快提交证明材料完成验证'
                        if risk_order else ''),
        'orderTime': _order_time_from_epoch(
            m.group(1), site, utc_offset_minutes) if m else '',
    }


def friendly_carrier(raw, tracks):
    """shipping_method_real 常是内部编码（如 JTmJT-NLU3-HLE-PB-CBN-a-Na），
    按已验证的单号前缀规律转成同事认识的承运商名：
    JMX 开头 = JT；49 开头 14 位 = IMILE（04 文档实测规律）。"""
    for t in tracks:
        if t.startswith('JMX'):
            return 'JT'
        if re.fullmatch(r'49\d{12}', t):
            return 'IMILE'
    upper = (raw or '').upper()
    if 'SPX' in upper:
        return 'SpeedX'
    if 'GOFO' in upper:
        return 'GOFO'
    return raw[:25] if raw else ''


class QueryOrchestrator(object):
    """环境查询编排：并发 worker 池，每个 worker 串行处理 start → 列表页
    → 详情页 → stop。

    并发安全性：多个不同环境同时开着是安全的（登录态覆盖的禁区是同一
    环境双开）；每个环境独立出口 IP 与指纹，互相无干扰。行状态更新全部
    走 self.lock；rows 状态列表 + lock 向 HTTP 层暴露进度；stop_event
    置位后各 worker 当前环境跑完即停。
    """

    def __init__(self, hub, log_dir=None, settle_seconds=6.0,
                 env_interval=1.0, concurrency=1):
        self.hub = hub
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.rows = []            # 每行 dict，见 _blank_row
        self.running = False
        self._inflight = set()    # 正在查询中的环境序号
        self.started_at = None
        self.finished_at = None
        self.fatal_error = ''
        self.fatal_error_code = ''
        self._screenshot_temp = None
        self._screenshots = {}
        self.log_dir = log_dir
        self.settle_seconds = settle_seconds
        self.env_interval = env_interval
        self.concurrency = max(1, min(5, int(concurrency or 1)))
        self.site = 'MX'
        # 并发 worker 的 browser/start 阶段全局串行：HubStudio 对同一/多个
        # 同时到达的 start 处理有先后，串行启动可避免 -10005 冲突
        self._start_lock = threading.Lock()
        # 需要"先关闭再重查"的环境序号（清理上次查询中断残留的孤儿窗口）
        self._force_stops = set()

    # ---- 行状态 ----

    def _blank_row(self, serial, site=None):
        site = normalize_site(site or self.site)
        return {'serial': str(serial), 'envName': '', 'state': 'pending',
                'site': site, 'siteName': SITE_LABELS[site],
                'orderNo': '', 'orderTime': '', 'amount': '', 'status': '',
                'statusCn': '', 'stage': '', 'tracks': [], 'pkgs': [],
                'carrier': '', 'kanDan': False, 'riskOrder': False,
                'riskMessage': '', 'ip': '', 'time': '',
                'timeZone': '', 'utcOffsetMinutes': None,
                'error': '', 'screenshotState': 'pending',
                'screenshotFile': '', 'screenshotError': '',
                'screenshotSizeKb': 0, 'screenshotWidth': 0,
                'screenshotHeight': 0}

    def snapshot(self):
        with self.lock:
            end_at = time.time() if self.running else self.finished_at
            elapsed = int(max(0, end_at - self.started_at)) \
                if self.started_at and end_at else 0
            return {
                'running': self.running,
                'current': ', '.join(sorted(self._inflight)),
                'elapsedSec': elapsed,
                'site': self.site,
                'siteName': SITE_LABELS[self.site],
                'fatalError': self.fatal_error,
                'fatalErrorCode': self.fatal_error_code,
                'rows': [dict(r) for r in self.rows],
            }

    def _update(self, row, **kw):
        with self.lock:
            row.update(kw)
            row['time'] = _query_timestamp(
                row.get('site') or self.site, row.get('utcOffsetMinutes'))

    def _save_snapshot(self, serial, tag, content):
        """查询异常时留存页面快照，便于事后排查。"""
        if not self.log_dir or not content:
            return
        try:
            os.makedirs(self.log_dir, exist_ok=True)
            path = os.path.join(
                self.log_dir, '%s_%s_%s.html' % (
                    time.strftime('%Y%m%d_%H%M%S'), serial, tag))
            with open(path, 'w', encoding='utf-8') as f:
                f.write(content)
        except Exception:
            pass

    # ---- 物流轨迹截图 ----

    def _reset_screenshots(self):
        old = self._screenshot_temp
        self._screenshot_temp = tempfile.TemporaryDirectory(
            prefix='purchase_tool_tracking_')
        self._screenshots = {}
        if old is not None:
            try:
                old.cleanup()
            except Exception:
                pass

    def _remove_screenshot(self, serial):
        path = self._screenshots.pop(str(serial), None)
        if path:
            try:
                os.remove(path)
            except OSError:
                pass

    def screenshot_bytes(self, serial):
        """读取当前批次某环境的轨迹截图；路径不进入 API/日志。"""
        with self.lock:
            path = self._screenshots.get(str(serial))
        if not path:
            return None
        try:
            with open(path, 'rb') as f:
                return f.read()
        except OSError:
            return None

    def close(self):
        old = self._screenshot_temp
        self._screenshot_temp = None
        self._screenshots = {}
        if old is not None:
            try:
                old.cleanup()
            except Exception:
                pass

    def _capture_tracking(self, page, serial, order_no, tracks, site='MX'):
        """打开 SHEIN 轨迹页并截取无地址/商品的物流区域。"""
        profile = _site_profile(site)
        page.goto(profile['orderTrackUrl'] % order_no,
                  settle_seconds=min(4.0, self.settle_seconds))
        if 'login' in page.url:
            raise RuntimeError('轨迹页登录失效')
        if not page.wait_selector('.track-steps-content', timeout=25):
            raise RuntimeError('轨迹页未加载出物流节点')
        track_html = page.outer_html()
        carrier_match = RE_CARRIER_NAME.search(track_html)
        data, width, height = page.capture_element_union(
            ['.logistics-container-wrapper', '.track-content-card__header',
             '.track-steps-content'],
            image_format='jpeg', quality=75, padding=8,
            hide_selectors=[
                '.shipping-information-new', '.top-address',
                '.track-goods-new', '.orders-track-page__header'])
        # 极少数超长轨迹若超过 300KB，以质量 60 重压一次。
        if len(data) > 300 * 1024:
            data, width, height = page.capture_element_union(
                ['.logistics-container-wrapper',
                 '.track-content-card__header', '.track-steps-content'],
                image_format='jpeg', quality=60, padding=8,
                hide_selectors=[
                    '.shipping-information-new', '.top-address',
                    '.track-goods-new', '.orders-track-page__header'])
        if self._screenshot_temp is None:
            self._reset_screenshots()
        safe_serial = re.sub(r'[^A-Za-z0-9_-]', '_', str(serial))[:40]
        suffix = re.sub(r'[^A-Za-z0-9]', '', str(tracks[0]))[-4:] \
            if tracks else 'none'
        filename = '环境%s_物流尾号%s.jpg' % (safe_serial, suffix)
        path = os.path.join(self._screenshot_temp.name, filename)
        with open(path, 'wb') as f:
            f.write(data)
        with self.lock:
            self._screenshots[str(serial)] = path
        result = {
            'screenshotState': 'ok', 'screenshotFile': filename,
            'screenshotError': '',
            'screenshotSizeKb': max(1, int(round(len(data) / 1024.0))),
            'screenshotWidth': width, 'screenshotHeight': height,
        }
        if carrier_match:
            result['carrier'] = carrier_match.group(1)
        return result

    # ---- 查询入口 ----

    def preflight_batch(self, serials, env_index=None, site='MX'):
        """Prove one eligible environment can launch before accepting a batch.

        A healthy ``group/list`` response does not prove that HubStudio has a
        usable browser core.  This reversible start/stop probe keeps a broken
        installation from creating a formal run and then failing every row.
        """
        site = normalize_site(site)
        serials = [str(serial) for serial in serials]
        with self.lock:
            if self.running:
                raise RuntimeError('已有查询在进行中')
        index = env_index or {
            str(env.get('serialNumber')): env for env in self.hub.env_list()
            if env.get('serialNumber') is not None
        }
        try:
            open_codes = self.hub.open_container_codes()
        except HubApiError as exc:
            self._record_systemic_hub_failure(exc, fail_rows=False)
            raise
        target_code = ''
        for serial in serials:
            env = index.get(serial)
            if not env:
                continue
            env_name = str(env.get('containerName') or '')
            env_site = re.search(r'-(MX|US)-', env_name, re.I)
            if env_site and env_site.group(1).upper() != site:
                continue
            code = str(env.get('containerCode') or '').strip()
            if code and code not in open_codes:
                target_code = code
                break
        if not target_code:
            return {'checked': False}

        started = False
        try:
            with self._start_lock:
                data = self.hub.browser_start(target_code, headless=True)
            started = True
            try:
                debugging_port = int((data or {}).get('debuggingPort'))
            except (TypeError, ValueError):
                debugging_port = 0
            if debugging_port < 1:
                raise HubApiError(
                    'HubStudio 启动环境后未返回调试端口',
                    'hubstudio_browser_launch_invalid')
            return {'checked': True, 'debuggingPort': debugging_port}
        except HubApiError as exc:
            self._record_systemic_hub_failure(exc, fail_rows=False)
            raise
        finally:
            if started:
                try:
                    self.hub.browser_stop(target_code)
                except HubApiError as exc:
                    self._record_systemic_hub_failure(exc, fail_rows=False)
                    raise

    def start_batch(self, serials, env_index=None, site='MX',
                    on_finished=None):
        """启动批量查询线程。env_index: {serialNumber(str): env dict}。"""
        site = normalize_site(site)
        serials = list(serials)
        self._prepare_run(serials, site, fresh=True)
        threading.Thread(
            target=self._run,
            args=(serials, env_index or {}, False, site, on_finished, True),
            daemon=True).start()

    def _prepare_run(self, serials, site, fresh):
        """Publish running state before background I/O can block or finish."""
        with self.lock:
            if self.running:
                raise RuntimeError('已有查询在进行中')
            self.stop_event = threading.Event()
            self.site = site
            self.running = True
            self.started_at = time.time()
            self.finished_at = None
            self.fatal_error = ''
            self.fatal_error_code = ''
            self._inflight = set()
            if fresh:
                self._reset_screenshots()
                self.rows = [self._blank_row(s, site) for s in serials]

    def requery(self, serial, env_index=None, force=False, on_finished=None):
        """单行重新查询（复用同一套流程，不新增行）。

        force=True：环境浏览器处于打开状态时先关闭再查——用于清理上次
        查询中断残留的孤儿窗口（-10005 放弃后启动完成的窗口）。
        """
        if self.running:
            raise RuntimeError('查询进行中，无法重查单个环境')
        serial = str(serial)
        with self.lock:
            row = next((r for r in self.rows if r['serial'] == serial),
                       None)
        if row is None:
            raise ValueError('该序号不在当前结果中，请重新发起批量查询')
        if force:
            self._force_stops.add(serial)
        site = row.get('site') or self.site
        self._prepare_run([serial], site, fresh=False)
        threading.Thread(
            target=self._run,
            args=([serial], env_index or {}, False, site, on_finished, True),
            daemon=True).start()

    def requery_failed(self, env_index=None, on_finished=None):
        """批量重查异常行（失败/使用中/未查询/已停止）。

        登录失效行不含在内——需先在 HubStudio 手动登录，否则重查结果不变。
        返回本次重查的行数。
        """
        if self.running:
            raise RuntimeError('查询进行中，无法重查')
        with self.lock:
            serials = [r['serial'] for r in self.rows
                       if r['state'] in ('fail', 'inuse', 'pending',
                                         'stopped')]
        if not serials:
            raise ValueError('没有可重查的异常行（失败 / 使用中 / 未查询）')
        with self.lock:
            site = next((r.get('site') for r in self.rows
                         if r['serial'] in serials), self.site)
        self._prepare_run(serials, site, fresh=False)
        threading.Thread(
            target=self._run,
            args=(serials, env_index or {}, False, site, on_finished, True),
            daemon=True).start()
        return len(serials)

    def request_stop(self):
        self.stop_event.set()

    # ---- 主流程 ----

    def _run(self, serials, env_index, fresh=True, site='MX',
             on_finished=None, prepared=False):
        site = normalize_site(site)
        if not prepared:
            self._prepare_run(serials, site, fresh=fresh)
        try:
            if not env_index:
                try:
                    env_index = {str(e.get('serialNumber')): e
                                 for e in self.hub.env_list()}
                except Exception as e:
                    self._fail_all('读取环境列表失败：%s' % e)
                    return
            try:
                open_codes = self.hub.open_container_codes()
            except Exception:
                open_codes = set()

            work = queue_mod.Queue()
            for s in serials:
                work.put(s)

            def worker():
                while not self.stop_event.is_set():
                    try:
                        serial = work.get_nowait()
                    except queue_mod.Empty:
                        return
                    with self.lock:
                        row = next((r for r in self.rows
                                    if r['serial'] == str(serial)), None)
                    if row is None:
                        continue
                    with self.lock:
                        self._inflight.add(str(serial))
                    try:
                        self._query_one(row, str(serial), env_index,
                                        open_codes, site)
                    finally:
                        with self.lock:
                            self._inflight.discard(str(serial))
                    if self.env_interval:
                        time.sleep(self.env_interval)

            n_workers = max(1, min(self.concurrency, len(serials)))
            threads = [threading.Thread(target=worker, daemon=True)
                       for _ in range(n_workers)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        finally:
            with self.lock:
                self.finished_at = time.time()
                self.running = False
            if on_finished:
                try:
                    on_finished()
                except Exception:
                    pass

    def _fail_all(self, reason):
        with self.lock:
            for r in self.rows:
                if r['state'] in ('pending', 'running'):
                    r.update(state='fail', error=reason,
                             time=_query_timestamp(
                                 r.get('site') or self.site,
                                 r.get('utcOffsetMinutes')))

    def _record_systemic_hub_failure(self, exc, fail_rows=True):
        code = str(getattr(exc, 'reason_code', '') or '')
        if code not in SYSTEMIC_HUB_FAILURE_CODES:
            return False
        message = str(exc)[:180]
        marker = getattr(self.hub, 'mark_runtime_failure', None)
        if callable(marker):
            try:
                marker(code, message)
            except Exception:
                pass
        with self.lock:
            if not self.fatal_error_code:
                self.fatal_error_code = code
                self.fatal_error = message
        self.stop_event.set()
        if fail_rows:
            self._fail_all('批次已终止：%s' % message)
        return True

    def _read_text_stable(self, page, ready_re=None, cf_wait=30, step=3):
        """读页面文本。

        - 命中 Cloudflare 安全校验页时，轮询等待其自动通过；
        - ready_re 提供时（如订单号正则），等目标内容出现——验证页刚清除
          时页面仍在水合，立即读会缺字段；目标出现后再多等 3 秒读全。
        """
        text = page.inner_text()
        deadline = time.time() + cf_wait
        while time.time() < deadline:
            if RE_CF.search(text):
                time.sleep(step)
                text = page.inner_text()
                continue
            if ready_re is not None and not ready_re.search(text):
                time.sleep(2)
                text = page.inner_text()
                continue
            break
        if ready_re is not None and ready_re.search(text):
            time.sleep(3)
            text = page.inner_text()
        return text

    def _start_browser(self, code):
        """启动浏览器：全局串行 + 对 -10005（上次 start 未执行完）耐心等待。

        -10005 常见于并发场景或上一批次 start 尚在执行时同环境再次 start，
        正确处理是等待其执行完而非快速重试（默认 0.4s×3 次的重试对它无效）。
        """
        deadline = time.time() + 90
        with self._start_lock:
            if self.stop_event.is_set() and self.fatal_error_code:
                raise HubApiError(
                    self.fatal_error or 'HubStudio 系统状态不可用',
                    self.fatal_error_code)
            while True:
                try:
                    return self.hub.browser_start(code, headless=True)
                except HubApiError as e:
                    if '-10005' in str(e) and time.time() < deadline:
                        time.sleep(5)
                        continue
                    self._record_systemic_hub_failure(e)
                    raise

    def _query_one(self, row, serial, env_index, open_codes, site='MX'):
        site = normalize_site(site)
        profile = _site_profile(site)
        self._remove_screenshot(serial)
        self._update(
            row, screenshotState='pending', screenshotFile='',
            screenshotError='', screenshotSizeKb=0,
            screenshotWidth=0, screenshotHeight=0)
        env = env_index.get(serial)
        if env is None:
            self._update(row, state='fail', error='未找到该环境序号')
            return
        code = str(env.get('containerCode'))
        env_name = env.get('containerName') or ''
        self._update(row, envName=env_name,
                     state='running')
        env_site = re.search(r'-(MX|US)-', env_name, re.I)
        if env_site and env_site.group(1).upper() != site:
            self._update(
                row, state='fail', error=(
                    '环境名显示为 %s 站，与所选 %s 站不一致；已跳过，未打开环境'
                    % (env_site.group(1).upper(), site)))
            return
        if code in open_codes:
            if serial in self._force_stops:
                # 孤儿窗口清理：上次查询中断残留的打开状态，关闭后重查
                self._force_stops.discard(serial)
                try:
                    self.hub.browser_stop(code)
                    time.sleep(3)
                except Exception as e:
                    self._update(row, state='fail',
                                 error='关闭残留窗口失败：%s' % str(e)[:80])
                    return
            else:
                # 正在使用（采购同事开着或未归档）——跳过防登录态覆盖
                self._update(row, state='inuse', error=(
                    '环境浏览器处于打开状态：若为上次查询中断的残留窗口，'
                    '请用「关闭并重查」；若有人正在使用请勿关闭'))
                return
        started = False
        try:
            data = self._start_browser(code)
            started = True
            port = int(data.get('debuggingPort'))
            ip = data.get('ip') or ''
            cdp = CdpClient(port)
            page = cdp.new_page()
            try:
                time_zone = page._evaluate(
                    'Intl.DateTimeFormat().resolvedOptions().timeZone') or ''
                # JS getTimezoneOffset 是“当地时间到 UTC”的分钟数，转换为
                # Python timezone 需要取反，例如 Chicago 夏令时 300 → -300。
                js_offset = page._evaluate('new Date().getTimezoneOffset()')
                utc_offset = -int(js_offset)
            except (TypeError, ValueError):
                time_zone = ''
                utc_offset = profile['utcOffsetMinutes']
            self._update(
                row, timeZone=time_zone, utcOffsetMinutes=utc_offset)

            # 1) 订单列表页：订单号 / 金额 / 状态 / 下单时间
            #    错误页与 Cloudflare 验证页均自动重试（最多 3 次）
            info, err = None, None
            for attempt in (1, 2, 3):
                page.goto(profile['ordersListUrl'],
                          settle_seconds=self.settle_seconds)
                text = self._read_text_stable(
                    page, ready_re=RE_ORDER_NO_BY_SITE[site])
                if 'login' in page.url:
                    self._save_snapshot(serial, 'list_login',
                                        page.outer_html())
                    self._update(row, state='login', ip=ip, error='')
                    return
                m = RE_ERR_PAGE.search(text)
                if m:
                    err = '%s，代理波动' % m.group(1)
                    self._save_snapshot(serial, 'list_err%d' % attempt,
                                        page.outer_html())
                    continue
                if RE_CF.search(text):
                    err = '安全验证未自动通过（Cloudflare）'
                    self._save_snapshot(serial, 'list_cf%d' % attempt,
                                        page.outer_html())
                    continue
                info = parse_list_page(text, site)
                break
            if info is None:
                self._update(row, state='fail', ip=ip,
                             error='页面加载失败（%s），可点重查' % err)
                return
            if not info['orderNo']:
                self._save_snapshot(serial, 'list', page.outer_html())
                self._update(row, state='fail', ip=ip,
                             error='未解析到订单号（该买家号可能还没有订单）')
                return
            self._update(row, ip=ip,
                         **{k: v for k, v in info.items() if v})

            # 2) 详情页：物流单号 / 承运商 / 砍单
            page.goto(profile['orderDetailUrl'] % info['orderNo'],
                      settle_seconds=self.settle_seconds)
            dtext = self._read_text_stable(page)
            if 'login' in page.url:
                self._save_snapshot(serial, 'detail_login', page.outer_html())
                self._update(row, state='login', error='')
                return
            m = RE_ERR_PAGE.search(dtext)
            if m:
                self._save_snapshot(serial, 'detail_err', page.outer_html())
                self._update(row, state='fail',
                             error='详情页加载失败（%s），可点重查' % m.group(1))
                return
            if RE_CF.search(dtext):
                self._save_snapshot(serial, 'detail_cf', page.outer_html())
                self._update(row, state='fail',
                             error='详情页安全验证未通过（Cloudflare），可点重查')
                return
            html = page.outer_html()
            detail = parse_detail_page(
                html, dtext, site, utc_offset_minutes=utc_offset)
            # 砍单双信号：列表状态或详情文案任一命中即判定
            refund_statuses = (
                {'Reembolsando'} if site == 'MX' else {'Refunding', 'Refunded'})
            if detail['kanDan'] and info['status'] not in refund_statuses:
                forced = 'Reembolsando' if site == 'MX' else 'Refunding'
                info = dict(info, status=forced,
                            statusCn=STATUS_CN[forced])
            if info['status'] in refund_statuses:
                detail['kanDan'] = True
            # 详情页风险验证是履约阻断，优先于列表页的 Paid；若已进入
            # 退款状态，则退款是更新、更终局的业务状态。
            if detail['riskOrder'] and not detail['kanDan']:
                info = dict(info, status='Risk verification',
                            statusCn=STATUS_CN['Risk verification'])
            if detail['tracks']:
                try:
                    screenshot = self._capture_tracking(
                        page, serial, info['orderNo'], detail['tracks'], site)
                except Exception as e:
                    screenshot = {
                        'screenshotState': 'fail', 'screenshotFile': '',
                        'screenshotError': str(e)[:100],
                        'screenshotSizeKb': 0,
                        'screenshotWidth': 0, 'screenshotHeight': 0,
                    }
            else:
                screenshot = {
                    'screenshotState': 'none', 'screenshotFile': '',
                    'screenshotError': '', 'screenshotSizeKb': 0,
                    'screenshotWidth': 0, 'screenshotHeight': 0,
                }
            # 列表页字段优先；详情页 orderTime 只在列表缺失时回填。
            # 先合并 dict，避免两个来源同时有 orderTime 时重复关键字报错。
            updates = dict(detail)
            updates.update({k: v for k, v in info.items() if v})
            updates.update(screenshot)
            self._update(row, state='ok', error='', **updates)
            page.close()
        except HubApiError as e:
            self._update(row, state='fail',
                         error='查询异常：%s' % str(e)[:120])
            self._record_systemic_hub_failure(e)
        except Exception as e:
            self._update(row, state='fail',
                         error='查询异常：%s' % str(e)[:120])
        finally:
            if started:
                try:
                    self.hub.browser_stop(code)
                except Exception:
                    pass
