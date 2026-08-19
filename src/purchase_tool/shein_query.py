# -*- coding: utf-8 -*-
"""SHEIN 墨西哥站页面解析 + 逐环境查询编排。

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

SITE = 'https://www.shein.com.mx'
ORDERS_LIST_URL = SITE + '/user/orders/list'
ORDER_DETAIL_URL = SITE + '/user/orders/detail/%s'
ORDER_TRACK_URL = SITE + '/orders/track?billno=%s'

# 状态取值（西班牙语）→ 中文
STATUS_CN = {
    'Procesando': '备货中', 'Empacando': '打包中', 'Enviado': '已发货',
    'Entregado': '已送达', 'Completado': '已完成', 'Cancelado': '已取消',
    'Reembolsando': '砍单退款中', 'Devolución': '退换货', 'No pagado': '未支付',
}

RE_ORDER_NO = re.compile(r'Núm\.?\s*de\s*pedido\s*([A-Z0-9]+)', re.I)
# 下单时间在列表页订单号文案之前，如 "17 Ago 2026 03:14:31Núm. de pedido XXX"
RE_ORDER_TIME = re.compile(
    r'(\d{1,2}\s+[A-Za-z]{3,}\s+\d{4}\s+\d{2}:\d{2}(?::\d{2})?)\s*Núm', re.I)
ES_MONTH = {'ene': '01', 'feb': '02', 'mar': '03', 'abr': '04', 'may': '05',
            'jun': '06', 'jul': '07', 'ago': '08', 'sep': '09',
            'oct': '10', 'nov': '11', 'dic': '12'}
RE_AMOUNT = re.compile(r'\$MXN[\d.,]+')
RE_STAGE = re.compile(
    r'Almac[eé]n[^.]*\.|En tr[áa]nsito[^.]*\.|Preparaci[óo]n[^.]*\.')
RE_SHIPPING_NO = re.compile(r'"shipping_no":"([^"]+)"')
RE_PACKAGE_NO = re.compile(r'"package_no":"([^"]+)"')
RE_CARRIER = re.compile(r'"shipping_method_real":"([^"]{2,40})"')
RE_KANDAN_TEXT = re.compile(r'Reembolsando|reembolso est[áa] siendo procesado')
# 大小写不敏感：页面出现过 EMPACANDO 大写形式（2026-08-18 实测 1007）
RE_STATUS_WORDS = re.compile(
    r'(Procesando|Empacando|Enviado|Reembolsando|Completado|Cancelado'
    r'|Entregado|Devoluci[óo]n|No pagado)', re.I)
# 部分订单的列表卡片不渲染下单时间，但详情页 SSR 会保留 addTime。
# 2026-08-19 用 1001 对照列表时间验证：addTime 为订单创建时间（秒），
# paymentTime 为支付完成时间（毫秒），仅在 addTime 缺失时降级使用。
RE_DETAIL_ADD_TIME = re.compile(r'"addTime"\s*:\s*"?(\d{10,13})"?')
RE_DETAIL_PAYMENT_TIME = re.compile(
    r'"paymentTime"\s*:\s*"?(\d{10,13})"?')
MX_TIMEZONE = timezone(timedelta(hours=-6))
# Chrome 网络错误页（如 ERR_CONNECTION_CLOSED，711 代理偶发波动）
RE_ERR_PAGE = re.compile(r'(ERR_[A-Z_]+)')
# Cloudflare 安全校验页（2026-08-18 实测 1007：验证会自动通过但需额外等待）
RE_CF = re.compile(
    r'Verificaci[óo]n de seguridad|Checking your browser'
    r'|Attention Required|Verifica que (eres|no eres)', re.I)


def _query_timestamp():
    """返回墨西哥站当地时间，与 SHEIN 页面的下单时间同时区。"""
    return datetime.now(MX_TIMEZONE).strftime('%Y-%m-%d %H:%M:%S')


def _uniq(items):
    seen, out = set(), []
    for x in items:
        if x and x not in seen:
            seen.add(x)
            out.append(x)
    return out


def _norm_order_time(raw):
    """'17 Ago 2026 03:14:31' → '2026-08-17 03:14:31'（西班牙月转数字）。"""
    m = re.match(r'(\d{1,2})\s+([A-Za-z]{3,})\s+(\d{4})\s+'
                 r'(\d{2}:\d{2}(?::\d{2})?)', raw or '')
    if not m:
        return raw or ''
    day, mon, year, hm = m.groups()
    mm = ES_MONTH.get(mon[:3].lower())
    return '%s-%s-%02d %s' % (year, mm, int(day), hm) if mm else raw


def _order_time_from_epoch(raw):
    """SHEIN SSR Unix 时间戳 → 墨西哥站显示时间（UTC-6）。"""
    try:
        stamp = int(raw)
        if stamp >= 10 ** 12:
            stamp /= 1000.0
        return datetime.fromtimestamp(
            stamp, timezone.utc).astimezone(MX_TIMEZONE).strftime(
                '%Y-%m-%d %H:%M:%S')
    except (TypeError, ValueError, OverflowError):
        return ''


def parse_list_page(text):
    """订单列表页解析。返回 {orderNo, orderTime, amount, status, statusCn, stage}。"""
    out = {'orderNo': None, 'orderTime': '', 'amount': None, 'status': None,
           'statusCn': None, 'stage': None}
    order_match = RE_ORDER_NO.search(text)
    if order_match:
        out['orderNo'] = order_match.group(1)
    m = RE_ORDER_TIME.search(text)
    if m:
        out['orderTime'] = _norm_order_time(m.group(1))
    m = RE_AMOUNT.search(text)
    if m:
        out['amount'] = m.group(0)
    if out['orderNo']:
        # 状态只取“当前订单号之后、详情按钮之前”的订单卡片区域。
        # 不能要求状态是区域最后一个词：1001 实测状态后还有仓库/包裹文案。
        detail_at = text.find('Detalles de Pedido', order_match.end())
        card_end = detail_at if detail_at >= 0 else len(text)
        card = text[order_match.end():card_end]
        m = RE_STATUS_WORDS.search(card)
        if m:
            word = m.group(1).capitalize()   # EMPACANDO → Empacando
            out['status'] = word
            out['statusCn'] = STATUS_CN.get(word, '')
        m = RE_STAGE.search(text)
        if m:
            out['stage'] = m.group(0)
    return out


def parse_detail_page(html, text):
    """订单详情页解析。列表缺时间时可用 SSR addTime 回填。"""
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
        'kanDan': bool(RE_KANDAN_TEXT.search(text)),
        'orderTime': _order_time_from_epoch(m.group(1)) if m else '',
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
        self._screenshot_temp = None
        self._screenshots = {}
        self.log_dir = log_dir
        self.settle_seconds = settle_seconds
        self.env_interval = env_interval
        self.concurrency = max(1, min(5, int(concurrency or 1)))
        # 并发 worker 的 browser/start 阶段全局串行：HubStudio 对同一/多个
        # 同时到达的 start 处理有先后，串行启动可避免 -10005 冲突
        self._start_lock = threading.Lock()
        # 需要"先关闭再重查"的环境序号（清理上次查询中断残留的孤儿窗口）
        self._force_stops = set()

    # ---- 行状态 ----

    def _blank_row(self, serial):
        return {'serial': str(serial), 'envName': '', 'state': 'pending',
                'orderNo': '', 'orderTime': '', 'amount': '', 'status': '',
                'statusCn': '', 'stage': '', 'tracks': [], 'pkgs': [],
                'carrier': '', 'kanDan': False, 'ip': '', 'time': '',
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
                'rows': [dict(r) for r in self.rows],
            }

    def _update(self, row, **kw):
        with self.lock:
            row.update(kw)
            row['time'] = _query_timestamp()

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

    def _capture_tracking(self, page, serial, order_no, tracks):
        """打开 SHEIN 轨迹页并截取无地址/商品的物流区域。"""
        page.goto(ORDER_TRACK_URL % order_no,
                  settle_seconds=min(4.0, self.settle_seconds))
        if 'login' in page.url:
            raise RuntimeError('轨迹页登录失效')
        if not page.wait_selector('.track-steps-content', timeout=25):
            raise RuntimeError('轨迹页未加载出物流节点')
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
        return {
            'screenshotState': 'ok', 'screenshotFile': filename,
            'screenshotError': '',
            'screenshotSizeKb': max(1, int(round(len(data) / 1024.0))),
            'screenshotWidth': width, 'screenshotHeight': height,
        }

    # ---- 查询入口 ----

    def start_batch(self, serials, env_index=None):
        """启动批量查询线程。env_index: {serialNumber(str): env dict}。"""
        if self.running:
            raise RuntimeError('已有查询在进行中')
        threading.Thread(
            target=self._run, args=(list(serials), env_index or {}),
            daemon=True).start()

    def requery(self, serial, env_index=None, force=False):
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
        threading.Thread(
            target=self._run, args=([serial], env_index or {}, False),
            daemon=True).start()

    def requery_failed(self, env_index=None):
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
        threading.Thread(
            target=self._run, args=(serials, env_index or {}, False),
            daemon=True).start()
        return len(serials)

    def request_stop(self):
        self.stop_event.set()

    # ---- 主流程 ----

    def _run(self, serials, env_index, fresh=True):
        self.stop_event = threading.Event()
        with self.lock:
            self.running = True
            self.started_at = time.time()
            self.finished_at = None
            self._inflight = set()
        try:
            if fresh:
                self._reset_screenshots()
                with self.lock:
                    self.rows = [self._blank_row(s) for s in serials]
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
                                        open_codes)
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

    def _fail_all(self, reason):
        with self.lock:
            for r in self.rows:
                if r['state'] in ('pending', 'running'):
                    r.update(state='fail', error=reason,
                             time=_query_timestamp())

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
            while True:
                try:
                    return self.hub.browser_start(code)
                except HubApiError as e:
                    if '-10005' in str(e) and time.time() < deadline:
                        time.sleep(5)
                        continue
                    raise

    def _query_one(self, row, serial, env_index, open_codes):
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
        self._update(row, envName=env.get('containerName') or '',
                     state='running')
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

            # 1) 订单列表页：订单号 / 金额 / 状态 / 下单时间
            #    错误页与 Cloudflare 验证页均自动重试（最多 3 次）
            info, err = None, None
            for attempt in (1, 2, 3):
                page.goto(ORDERS_LIST_URL, settle_seconds=self.settle_seconds)
                text = self._read_text_stable(page, ready_re=RE_ORDER_NO)
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
                info = parse_list_page(text)
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
            page.goto(ORDER_DETAIL_URL % info['orderNo'],
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
            detail = parse_detail_page(html, dtext)
            # 砍单双信号：列表状态或详情文案任一命中即判定
            if (detail['kanDan'] or info['status'] == 'Reembolsando') \
                    and info['status'] != 'Reembolsando':
                info = dict(info, status='Reembolsando',
                            statusCn=STATUS_CN['Reembolsando'])
            if detail['tracks']:
                try:
                    screenshot = self._capture_tracking(
                        page, serial, info['orderNo'], detail['tracks'])
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
        except Exception as e:
            self._update(row, state='fail',
                         error='查询异常：%s' % str(e)[:120])
        finally:
            if started:
                try:
                    self.hub.browser_stop(code)
                except Exception:
                    pass
