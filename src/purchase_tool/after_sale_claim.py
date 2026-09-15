# -*- coding: utf-8 -*-
"""SHEIN 买家端「包裹已送达但未收到」售后申请（扫描只读 + 提交写操作）。

业务背景：采购部对已下单买家号的「已送达但没收到货」订单，需要在 SHEIN
买家端逐单发起售后申请（理由固定「El paquete completo entregado pero no
recibido」，退款原路退回）。人工动线是：打开环境 → 进订单列表 → 找到卡片
上带售后入口的已送达订单 → 点入口 → 勾包裹 → 选退款路径 → 提交。

本模块把它拆成两段，与页面上的两步式操作一一对应：

1. ``start_scan``（**只读**）按环境序号扫描订单列表，逐单调用买家端
   ``refund_only/pre_info`` 判定「这个包裹现在还能不能申请售后」，产出
   可勾选清单。已提交过的包裹会被平台挪进 ``disable_package_list``，
   因此扫描阶段就能天然幂等地区分出「可申请 / 已提交过」。
2. ``start_submit``（**写操作**）只对人工勾选的行提交：直接进申请页 →
   勾可退包裹 → Confirmar → 切退款路径 → Presentar，成功后落库
   ``refund_bill_id``。

关键实测结论（20260915 真机全链路验证，环境 4902）：

- 卡片上的入口 ``Paquete entregado, no recibido`` 只是前端路由跳转
  （``location.href`` 到 ``/orders/refundApplication?billno=…``），点它
  本身不提交；真正的写操作在申请页的「Presentar」。
- 提交接口 ``refund_only/create`` 的 ``path`` 字段与界面单选项不是同一套
  取值：界面 1=SHEIN 钱包、2=原路退回、3=礼品卡，报文里走一层映射
  ``{1:2, 2:3, 3:1}``。本模块固定选「原路退回」（界面第 2 项）——页面
  默认选中的是 SHEIN 钱包，必须主动切换，否则退款会进钱包而不是原卡。
- 一个包裹一次只能勾一个（弹窗是单选 radio）；订单有多个可退包裹时要
  分多次提交，模块按「可退列表清空为止」循环。
- 页面装了沉浸式翻译扩展，会在原文后追加中文（``innerText`` 变成
  「Paquete entregado, no recibido 包裹已送达…」）。所有文案匹配一律走
  **去空白小写后的子串匹配**，不能用等值比较。
- 环境占用沿用店铺巡检的四层策略：谁开谁关、被人工切换重试一次后标
  ``inuse``、运行中拒绝重复发起、云端幂等键防并行重复提交。
"""
import hashlib
import json
import random
import re
import threading
import time
from datetime import datetime, timezone

from .cdp import CdpClient, CdpError
from .redaction import scrub_text

ORIGIN = 'https://www.shein.com.mx'
# 订单列表标签：status_type=3 即「Pedidos Enviados」（实测页面标题为
# 「Podidos Enviados」），已送达订单就落在这里，也是采购人工操作的入口。
ORDERS_STATUS_TYPE = 3
ORDERS_LIST_URL = '%s/user/orders/list?status_type=%d' % (
    ORIGIN, ORDERS_STATUS_TYPE)
REFUND_APPLY_URL = ORIGIN + \
    '/orders/refundApplication?billno=%s&pageFrom=orderList'

# 业务口径：退款必须原路退回（页面默认是 SHEIN 钱包，需主动切换）
REFUND_PATH_LABEL = 'Cuenta original de pago'
REFUND_PATH_LABEL_KEY = 'cuentaoriginaldepago'

ORDER_ENTRY_KEY = 'paqueteentregado,norecibido'
PACKAGE_SELECT_TEXT = 'seleccionar'
PRESENTAR_TEXT = 'presentar'

ORDER_NO_RE = re.compile(r'N[úu]m\.?\s*de\s*pedido\s*([A-Z0-9]{6,})', re.I)
# 「Entregado a」与日期之间可能被沉浸式翻译插入中文（实测「Entregado a
# 已交付给 04 Sep 2026 14:36:41」），因此中间允许非数字字符占位。
DELIVERED_RE = re.compile(
    r'Entregado a[^0-9]{0,24}'
    r'([0-9]{2}\s*[A-Za-zÀ-ÿ]{3}\s*[0-9]{4}(?:\s*[0-9:]{4,8})?)', re.I)
AMOUNT_RE = re.compile(r'\$MXN\s*([\d,]+\.?\d*)')
REFUND_DONE_RE = re.compile(
    r'Procesamiento de reembolsos|En revisi[óo]n vendedor', re.I)

SCAN_RUNNING_STATES = ('queued', 'running')
CLAIM_RUNNING_STATES = ('queued', 'running')

# 提交成功后前端跳转的落地页，URL 里带 refund_bill_id_list=<单号>_<退款单ID>
REFUND_LABEL_MARK = '/orders/refundLabel/'
REFUND_BILL_ID_RE = re.compile(r'refund_bill_id_list=([A-Z0-9]+)_(\d+)')

_JS_NORM = ('const norm=s=>String(s||"").replace(/\\s+/g,"")'
            '.toLocaleLowerCase();')

# 把点击点钳制在「元素与视口交集」内：弹窗/长页里元素中心可能落到视口
# 外，直接派发中心坐标会点空。与 cdp.py 的 _JS_CLAMPED_POINT 同一套算法。
_JS_CLAMP = (
    'const clamp=(e)=>{'
    'if(e.scrollIntoViewIfNeeded){try{e.scrollIntoViewIfNeeded(true);}'
    'catch(err){}}'
    'const rr=e.getBoundingClientRect();'
    'const m=Math.max(2,Math.min(12,rr.width/4,rr.height/4));'
    'const x=Math.min(Math.max(rr.left+rr.width/2,rr.left+m,m),'
    'Math.min(rr.right-m,innerWidth-m));'
    'const y=Math.min(Math.max(rr.top+rr.height/2,rr.top+m,m),'
    'Math.min(rr.bottom-m,innerHeight-m));'
    'if(x<rr.left||x>rr.right||y<rr.top||y>rr.bottom)return null;'
    'return {x:x,y:y};};')

# 订单卡片清单：只回传卡片原文与「是否有售后入口」，字段解析放 Python
# （parse_order_card 可脱浏览器单测）。状态词只取卡片内文本——历史坑是
# 全局正则会命中顶部标签栏的 Enviado。
_JS_SCAN_ORDERS = ('(() => {' + _JS_NORM + '''
  const out=[]; const cards=[...document.querySelectorAll("li.list-item")]
    .filter(n=>/N[úu]m\\.?\\s*de\\s*pedido/i.test(n.innerText||""));
  for (const li of cards) {
    const t = li.innerText || "";
    if (!/N[úu]m\\.?\\s*de\\s*pedido/i.test(t)) continue;
    const entry = [...li.querySelectorAll("a")].some(a =>
      norm(a.innerText).indexOf(%s) >= 0);
    out.push({text: t.slice(0, 1200), hasEntry: entry});
  }
  return out; })()''')

# 申请页就绪标志：退款理由区块渲染出来即视为表单可用
_JS_APPLY_READY = (
    '(() => { const e=document.querySelector(".refund-apply-reason__content");'
    ' if(!e) return false;'
    ' const r=e.getBoundingClientRect(); return r.width>0 && r.height>0; })()')

_JS_REASON_TEXT = (
    '(() => { const e=document.querySelector(".refund-apply-reason__content");'
    ' return e ? String(e.innerText||"").replace(/\\s+/g," ").trim() : ""; })()')

_JS_DIALOG_VISIBLE = (
    '(() => { const d=document.querySelector(".sui-dialog__wrapper");'
    ' if(!d) return false; const r=d.getBoundingClientRect();'
    ' return r.width>0 && r.height>0; })()')

_JS_DIALOG_CLOSED = (
    '(() => { const d=document.querySelector(".sui-dialog__wrapper");'
    ' if(!d) return true; const r=d.getBoundingClientRect();'
    ' return !(r.width>0 && r.height>0); })()')

# 弹窗内「可退包裹」的勾选点。不可退包裹挂在 .disabled-package-tip 下且
# radio 带 disabled，天然不在 .sui-radio-group 里，无需额外过滤。
_JS_PICK_POINTS = ('(() => {' + _JS_NORM + _JS_CLAMP + '''
  const pts=[];
  const items=[...document.querySelectorAll(
    ".sui-dialog__wrapper .sui-radio-group .refund-package-item")];
  for (const it of items) {
    const lab=it.querySelector("label.sui-radio")||it;
    const inp=it.querySelector("input[type=radio]");
    if (inp && inp.disabled) continue;
    const p=clamp(lab);
    if (p) pts.push({x:p.x, y:p.y,
      packageNo: String((inp&&inp.value)||"")});
  }
  return pts; })()''')

_JS_DISABLED_PACKAGES = (
    '(() => [...document.querySelectorAll('
    '".sui-dialog__wrapper .refund-package-item.is-disabled input[type=radio]")]'
    '.map(i=>String(i.value||"")))()')

_JS_CHECKED_PACKAGES = (
    '(() => [...document.querySelectorAll('
    '".sui-dialog__wrapper .sui-radio-group input[type=radio]")]'
    '.filter(i=>i.checked).map(i=>String(i.value||"")))()')

_JS_CONFIRM_ENABLED = (
    '(() => { const b=document.querySelector(".sui-dialog__footer button");'
    ' return !!b && !b.disabled; })()')

_JS_SELECT_DESC = (
    '(() => { const e=document.querySelector(".refund-package-select__desc");'
    ' return e ? String(e.innerText||"").replace(/\\s+/g," ").trim() : ""; })()')

_JS_PATH_PRESENT = (
    '(() => [...document.querySelectorAll(".refund-path-option")].length)()')

# 退款路径选项的勾选点：按标题子串定位，返回该项与它的单选框状态
_JS_PATH_POINT = ('(() => {' + _JS_NORM + _JS_CLAMP + '''
  const opts=[...document.querySelectorAll(".refund-path-option")];
  for (const o of opts) {
    if (norm(o.innerText).indexOf(%s) < 0) continue;
    const inp=o.querySelector("input[type=radio]");
    const p=clamp(inp||o);
    if (!p) return null;
    return {x:p.x, y:p.y, checked: !!(inp&&inp.checked),
      title: String(o.innerText||"").replace(/\\s+/g," ").trim().slice(0,80)};
  }
  return null; })()''')

# 退款路径是否已选中：不要靠固定 sleep 猜渲染完成，直接等它真被选中
_JS_PATH_CHECKED = ('(() => {' + _JS_NORM + '''
  const opts=[...document.querySelectorAll(".refund-path-option")];
  for (const o of opts) {
    if (norm(o.innerText).indexOf(%s) < 0) continue;
    const inp=o.querySelector("input[type=radio]");
    return !!(inp && inp.checked);
  }
  return false; })()''')

_JS_PRESENTAR_ENABLED = (
    '(() => { const bs=[...document.querySelectorAll('
    '".order-refund-apply__footer button")];'
    ' const b=bs.find(x=>/presentar/i.test(x.innerText||""))||bs[0];'
    ' if(!b) return false; const r=b.getBoundingClientRect();'
    ' return !b.disabled && r.width>0 && r.height>0; })()')

# 结果提示（成功/失败共用）。沉浸式翻译会追加中文，一律只取西语原文段。
_JS_TOAST_TEXT = ('(() => {' + _JS_NORM + '''
  const nodes=[...document.querySelectorAll(
    "[class*=toast],[class*=message],[class*=notify]")];
  const texts=nodes.map(n=>String(n.innerText||"").replace(/\\s+/g," ").trim())
    .filter(Boolean);
  return texts.join(" | ").slice(0,240); })()''')

# 退款账户（原路退回落到哪张卡）：退款成功页的账户区块，实测形如「****2281」。
# 该区块在账户明细接口返回空时会渲染成占位文案 Error，因此取值后要能识别并丢弃。
_JS_REFUND_ACCOUNT = (
    '(() => { const e=document.querySelector(".refundAccount-info .tip");'
    ' if(!e) return "";'
    ' const t=String(e.innerText||"").replace(/\\s+/g," ").trim();'
    ' return /^[*0-9\\s-]{4,24}$/.test(t) ? t : ""; })()')

_JS_TO_REFUND_LABEL = (
    '(() => location.href.indexOf("' + REFUND_LABEL_MARK + '") >= 0)()')


def _norm_key(text):
    """与页面内 norm() 一致：去空白 + 小写，用于翻译安全的文案比对。"""
    return re.sub(r'\s+', '', str(text or '')).lower()


def parse_order_card(text):
    """从订单卡片文本解析单号/送达时间/金额/是否已提交售后。

    状态词只取卡片内文本，避免命中顶部标签栏（SHEIN 列表页老坑）。
    """
    raw = str(text or '')
    match = ORDER_NO_RE.search(raw)
    if not match:
        return None
    delivered = DELIVERED_RE.search(raw)
    amount = AMOUNT_RE.search(raw)
    return {
        'orderNo': match.group(1),
        'deliveredAt': delivered.group(1).strip() if delivered else '',
        'amount': amount.group(1) if amount else '',
        'refundInProgress': bool(REFUND_DONE_RE.search(raw)),
    }


def refund_bill_id_from_url(url):
    """从成功页 URL 取 ``(订单号, 退款单ID)``；不在成功页返回 None。

    成功页形如：``/orders/refundLabel/<billno>?refund_bill_id_list=
    <billno>_<refund_bill_id>``。
    """
    match = REFUND_BILL_ID_RE.search(str(url or ''))
    return (match.group(1), match.group(2)) if match else None


class AfterSaleClaimer(object):
    """售后扫描与提交编排：后台线程驱动，snapshot() 供进度轮询。"""

    def __init__(self, hub, log=None, headless=False,
                 stagger_seconds=1.5, order_stagger=(5.0, 15.0)):
        self.hub = hub
        self.headless = bool(headless)
        self._stagger = max(0.0, float(stagger_seconds))
        low, high = order_stagger or (0.0, 0.0)
        self._order_stagger = (max(0.0, float(low)), max(0.0, float(high)))
        self._log = log or (lambda msg: None)
        self._lock = threading.Lock()
        self._mode = ''
        self._running = False
        self._stop_event = threading.Event()
        self._scan_rows = {}
        self._claim_rows = {}
        self._screenshots = {}

    # ---- 对外入口（本地 HTTP 端点消费） ----

    def start_scan(self, serials, browser_mode=None, headless=None):
        """启动一批只读扫描；运行中重复发起会被拒绝。"""
        return self._start('scan', serials, browser_mode, headless)

    def start_submit(self, items, browser_mode=None, headless=None):
        """启动一批售后提交；items 为 ``[{environmentSerial, orderNo}]``。"""
        cleaned = []
        for item in items or []:
            if not isinstance(item, dict):
                continue
            serial = str(item.get('environmentSerial') or '').strip()
            order_no = str(item.get('orderNo') or '').strip()
            if not (serial and order_no):
                continue
            cleaned.append({
                'environmentSerial': serial,
                'orderNo': order_no,
                'storeName': str(item.get('storeName') or '').strip()[:128],
                'packageNo': str(item.get('packageNo') or '').strip()[:64],
                # 送达时间与商品图来自扫描阶段，随勾选一起带下来
                'deliveredAt': str(item.get('deliveredAt') or '').strip()[:32],
                'goodsImg': str(item.get('goodsImg') or '').strip()[:300],
            })
        return self._start('claim', cleaned, browser_mode, headless)

    def request_stop(self):
        self._stop_event.set()
        return {'stopRequested': True}

    def snapshot(self):
        """扫描与提交两个视图合并返回，前端按 mode 分别渲染。"""
        with self._lock:
            scan_rows = [dict(r) for r in self._scan_rows.values()]
            claim_rows = [dict(r) for r in self._claim_rows.values()]
            running = self._running
            mode = self._mode
        scan_rows.sort(key=lambda r: (str(r.get('environmentSerial') or ''),
                                      str(r.get('orderNo') or '')))
        for row in scan_rows:
            row['screenshotStatus'] = 'ok' if row.get('screenshotSha256') \
                else ''
        claim_rows.sort(key=lambda r: str(r.get('orderNo') or ''))
        for row in claim_rows:
            row['screenshotStatus'] = 'ok' if row.get('screenshotSha256') \
                else ''
        return {'running': running, 'mode': mode, 'rows': scan_rows,
                'claimRows': claim_rows}

    def screenshot_bytes(self, key):
        with self._lock:
            return self._screenshots.get(str(key))

    # ---- 批次编排 ----

    def _start(self, mode, items, browser_mode, headless=None):
        with self._lock:
            if self._running:
                return {'running': True, 'mode': self._mode,
                        'error': '已有售后任务运行中'}
            self._running = True
            self._mode = mode
            self._stop_event = threading.Event()
            self._screenshots = {}
            if mode == 'scan':
                self._scan_rows = {}
                keys = [str(s) for s in items if str(s or '').strip()]
            else:
                self._claim_rows = {}
                keys = [item['orderNo'] for item in items]
        if isinstance(headless, bool):
            self.headless = headless
        thread = threading.Thread(
            target=self._run_batch, args=(mode, items, browser_mode),
            daemon=True)
        thread.start()
        return {'running': True, 'mode': mode, 'total': len(keys)}

    def _run_batch(self, mode, items, browser_mode):
        try:
            headless = self.headless if browser_mode != 'visible' else False
            if mode == 'scan':
                self._run_scan(items, headless)
            else:
                self._run_claim(items, headless)
        except Exception as exc:  # 批次级异常也要把 running 落回 False
            self._log('售后批次异常: %s' % exc)
        finally:
            with self._lock:
                self._running = False

    def _env_index(self, keys):
        """把序号/环境ID/环境名解析成环境对象（重名环境要求改用序号）。"""
        wanted = {str(k).strip().casefold(): k for k in keys}
        index = {}
        name_matches = {}
        for env in self.hub.env_list():
            for identifier in (env.get('serialNumber'),
                               env.get('containerCode')):
                key = str(identifier or '').strip().casefold()
                if key and key in wanted:
                    index[wanted[key]] = env
            name = str(env.get('containerName') or '').strip()
            if name and name.casefold() in wanted:
                name_matches.setdefault(name.casefold(), []).append(env)
        for name_key, matched in name_matches.items():
            if len(matched) == 1:
                index.setdefault(wanted[name_key], matched[0])
        return index

    # ---- 扫描（只读） ----

    def _run_scan(self, serials, headless):
        env_index = self._env_index(serials)
        with self._lock:
            for serial in serials:
                env = env_index.get(serial, {})
                self._scan_rows[serial] = {
                    'environmentSerial': serial,
                    'environmentId': str(env.get('containerCode') or ''),
                    'storeName': env.get('containerName') or serial,
                    'accountName': self._account_name(env),
                    'status': 'queued',
                    'orderNo': '',
                    'deliveredAt': '',
                    'amount': '',
                    'packages': [],
                    'claimable': False,
                    'errorSummary': None,
                    'screenshotSha256': None,
                    'screenshotStatus': '',
                }
        threads = []
        for serial in serials:
            if self._stop_event.is_set():
                break
            thread = threading.Thread(
                target=self._scan_one_guarded,
                args=(serial, env_index.get(serial, {}), headless),
                daemon=True)
            thread.start()
            threads.append(thread)
            time.sleep(self._stagger)
        for thread in threads:
            thread.join()
        with self._lock:
            for serial in serials:
                row = self._scan_rows.get(serial)
                if row and row.get('status') in SCAN_RUNNING_STATES:
                    row['status'] = 'stopped'

    def _scan_one_guarded(self, serial, env, headless):
        try:
            self._scan_one(serial, env, headless)
        except Exception as exc:  # 单环境异常不拖垮整批
            self._log('扫描异常 %s: %s' % (serial, exc))

    def _scan_one(self, serial, env, headless):
        started = time.time()
        page = None
        opened_by_me = False
        try:
            page, opened_by_me = self._open_env(env, serial, headless)
            self._publish_scan(serial, {'status': 'running'})
            page.goto(ORDERS_LIST_URL, dom_timeout=45, settle_seconds=8.0)
            if self._login_required(page):
                self._fail_scan(serial, 'login',
                                '买家端未登录（环境登录态缺失，请先登录该环境）')
                return
            self._scroll_orders_list(page)
            cards = page.js_evaluate(_JS_SCAN_ORDERS % json.dumps(
                ORDER_ENTRY_KEY)) or []
            candidates = []
            for card in cards:
                if not isinstance(card, dict):
                    continue
                # 只对「已送达且有售后入口」的单子问接口：没有入口的单
                # 平台根本不给申请，问了也是白问。
                if not card.get('hasEntry'):
                    continue
                parsed = parse_order_card(card.get('text') or '')
                if not parsed:
                    continue
                parsed['packages'] = []
                parsed['blockedPackages'] = []
                parsed['claimable'] = False
                candidates.append(parsed)
            for candidate in candidates:
                info = self._pre_info(page, candidate['orderNo'])
                candidate['packages'] = info.get('eligible') or []
                candidate['blockedPackages'] = info.get('blocked') or []
                candidate['claimable'] = bool(candidate['packages'])
                candidate['reasonId'] = info.get('reasonId') or ''
                candidate['preInfoCode'] = info.get('code') or ''
            with self._lock:
                row = self._scan_rows.get(serial) or {}
                row.update({
                    'status': 'ok' if candidates else 'skip',
                    'orders': candidates,
                    'orderNo': candidates[0]['orderNo'] if candidates else '',
                    'deliveredAt': (candidates[0]['deliveredAt']
                                    if candidates else ''),
                    'amount': candidates[0]['amount'] if candidates else '',
                    'packages': (candidates[0]['packages']
                                 if candidates else []),
                    'claimable': any(c['claimable'] for c in candidates),
                    'durationSeconds': int(time.time() - started),
                    'errorSummary': None if candidates else (
                        '该环境订单列表没有可申请售后的已送达订单'),
                })
                self._scan_rows[serial] = row
        except Exception as exc:
            self._fail_scan(
                serial, 'fail',
                scrub_text('%s: %s' % (type(exc).__name__, str(exc)))[:200],
                page=page, started=started)
        finally:
            if opened_by_me:
                self._stop_env(env, serial)

    def _fail_scan(self, serial, status, reason, page=None, started=None):
        with self._lock:
            row = self._scan_rows.get(serial) or {}
            row.update({
                'status': status,
                'errorSummary': reason,
                'orders': row.get('orders') or [],
                'durationSeconds': (int(time.time() - started)
                                    if started else None),
            })
            self._scan_rows[serial] = row
        if page is not None:
            self._capture_screenshot(self._scan_rows, serial, page)

    # ---- 提交（写操作） ----

    def _run_claim(self, items, headless):
        env_index = self._env_index(
            [item['environmentSerial'] for item in items])
        with self._lock:
            for item in items:
                self._claim_rows[item['orderNo']] = {
                    'orderNo': item['orderNo'],
                    'environmentSerial': item['environmentSerial'],
                    'storeName': item.get('storeName')
                    or (env_index.get(item['environmentSerial'], {})
                        .get('containerName')) or '',
                    'status': 'queued',
                    'packageNo': item.get('packageNo') or '',
                    'refundBillId': '',
                    'refundPath': '',
                    'note': '',
                    'errorSummary': None,
                    'screenshotSha256': None,
                    'screenshotStatus': '',
                }
        # 同环境的多单合并到一次环境打开里跑完，避免反复开关环境。
        grouped = {}
        for item in items:
            grouped.setdefault(item['environmentSerial'], []).append(item)
        for serial, group in grouped.items():
            if self._stop_event.is_set():
                break
            env = env_index.get(serial, {})
            thread = threading.Thread(
                target=self._claim_env_guarded, args=(serial, env, group,
                                                      headless),
                daemon=True)
            thread.start()
            thread.join()  # 环境之间串行：写操作不并发
        with self._lock:
            for item in items:
                row = self._claim_rows.get(item['orderNo'])
                if row and row.get('status') in CLAIM_RUNNING_STATES:
                    row['status'] = 'stopped'

    def _claim_env_guarded(self, serial, env, items, headless):
        try:
            self._claim_env(serial, env, items, headless)
        except Exception as exc:
            self._log('提交异常 %s: %s' % (serial, exc))

    def _claim_env(self, serial, env, items, headless):
        page = None
        opened_by_me = False
        try:
            page, opened_by_me = self._open_env(env, serial, headless)
            for index, item in enumerate(items):
                if self._stop_event.is_set():
                    return
                if index:
                    # 同一环境内逐单串行并随机停顿，贴近人工节奏
                    time.sleep(random.uniform(*self._order_stagger))
                try:
                    self._claim_one(page, serial, item)
                except Exception as exc:
                    self._fail_claim(
                        item['orderNo'], 'fail',
                        scrub_text('%s: %s' % (type(exc).__name__,
                                               str(exc)))[:200], page)
        finally:
            if opened_by_me:
                self._stop_env(env, serial)

    def _claim_one(self, page, serial, item):
        order_no = item['orderNo']
        started = time.time()
        self._publish_claim(order_no, {'status': 'running'})
        url = REFUND_APPLY_URL % order_no
        page.goto(url, dom_timeout=45, settle_seconds=6.0)
        if self._login_required(page):
            self._fail_claim(order_no, 'login',
                             '买家端未登录（环境登录态缺失）', page)
            return
        if not page.wait_for(_JS_APPLY_READY, timeout=25):
            self._fail_claim(order_no, 'fail', '售后申请页未就绪', page)
            return
        reason = page.js_evaluate(_JS_REASON_TEXT) or ''
        # 二次体检：页面自己会带着当前状态问一次 pre_info，扫描结果可能
        # 已经过期（别人先提交了），这里是提交前的最后一道幂等闸门。
        info = self._pre_info(page, order_no)
        eligible = info.get('eligible') or []
        if not eligible:
            self._fail_claim(
                order_no, 'blocked',
                '该订单已无可申请售后的包裹（可能已提交过）', page)
            return
        submitted = []
        leftover = []
        guard = 0
        while eligible and guard < 5:
            guard += 1
            ok = self._submit_package(page, order_no)
            if not ok.get('ok'):
                self._fail_claim(order_no, 'fail', ok.get('reason') or '提交失败',
                                 page)
                return
            submitted.append(ok)
            remaining = (ok.get('remaining') or [])
            if not remaining:
                leftover = []
                break
            # 弹窗是单选：还有别的可退包裹时要重新进申请页再提交一次
            leftover = remaining
            eligible = remaining
            page.goto(url, dom_timeout=45, settle_seconds=5.0)
            if not page.wait_for(_JS_APPLY_READY, timeout=25):
                break
        last = submitted[-1] if submitted else {}
        with self._lock:
            row = self._claim_rows.get(order_no) or {}
            row.update({
                'status': 'ok',
                'packageNo': last.get('packageNo') or '',
                'refundBillId': last.get('refundBillId') or '',
                'refundPath': REFUND_PATH_LABEL,
                'deliveredAt': str(item.get('deliveredAt') or '')[:32],
                'goodsImg': str(item.get('goodsImg') or '')[:300],
                'refundAccount': last.get('refundAccount') or '',
                'packageCount': len(submitted),
                'reasonText': reason[:120],
                'note': ('仍有 %d 个可退包裹未提交（弹窗单选，需再次执行）'
                         % len(leftover)) if leftover else '',
                'submittedAt': datetime.now(timezone.utc).isoformat(),
                'durationSeconds': int(time.time() - started),
                'errorSummary': None,
            })
            self._claim_rows[order_no] = row
        self._capture_screenshot(self._claim_rows, order_no, page)

    def _submit_package(self, page, order_no):
        """在申请页完成「勾包裹 → Confirmar → 选原路退回 → Presentar」。"""
        # 1) 打开包裹弹窗
        if not self._click(page, '.refund-apply-package__header'):
            return {'ok': False, 'reason': '未找到包裹选择入口'}
        if not page.wait_for(_JS_DIALOG_VISIBLE, timeout=20):
            return {'ok': False, 'reason': '包裹选择弹窗未出现'}
        disabled = page.js_evaluate(_JS_DISABLED_PACKAGES) or []
        points = page.js_evaluate(_JS_PICK_POINTS) or []
        if not points:
            return {'ok': False, 'reason': '弹窗内没有可退包裹',
                    'blocked': disabled}
        for point in points:
            page.native_click_point(point['x'], point['y'])
            time.sleep(0.6)
        picked = page.js_evaluate(_JS_CHECKED_PACKAGES) or []
        if not picked:
            return {'ok': False, 'reason': '包裹勾选未生效'}
        package_no = picked[-1]
        if not page.wait_for(_JS_CONFIRM_ENABLED, timeout=15):
            return {'ok': False, 'reason': 'Confirmar 按钮未解禁'}
        if not self._click(page, '.sui-dialog__footer button'):
            return {'ok': False, 'reason': '未找到 Confirmar 按钮'}
        if not page.wait_for(_JS_DIALOG_CLOSED, timeout=20):
            return {'ok': False, 'reason': '包裹弹窗未关闭'}
        page.wait_for(
            '(() => { const e=document.querySelector('
            '".refund-package-select__desc");'
            ' return !!e && String(e.innerText||"").replace(/\\s+/g,"")'
            '.toLocaleLowerCase().indexOf("%s") < 0; })()'
            % PACKAGE_SELECT_TEXT, timeout=15)

        # 2) 切退款路径到「原路退回」（页面默认是 SHEIN 钱包，必须主动切）
        ok, reason = self._select_refund_path(page)
        if not ok:
            return {'ok': False, 'reason': reason, 'packageNo': package_no}

        # 3) 提交
        if not page.wait_for(_JS_PRESENTAR_ENABLED, timeout=20):
            return {'ok': False, 'reason': 'Presentar 按钮未解禁',
                    'packageNo': package_no}
        if not self._click(page, '.order-refund-apply__footer button'):
            return {'ok': False, 'reason': '未找到 Presentar 按钮',
                    'packageNo': package_no}
        landed = page.wait_for(_JS_TO_REFUND_LABEL, timeout=45)
        toast = page.js_evaluate(_JS_TOAST_TEXT) or ''
        if not landed:
            return {'ok': False, 'reason': '提交后未跳转成功页：%s'
                    % (toast or page.url)[:160], 'packageNo': package_no}
        bill = refund_bill_id_from_url(page.url)
        refund_account = self._read_refund_account(page)
        remaining = self._pre_info(page, order_no).get('eligible') or []
        return {'ok': True, 'packageNo': package_no,
                'refundBillId': bill[1] if bill else '',
                'refundAccount': refund_account,
                'toast': toast[:120], 'remaining': remaining}

    @staticmethod
    def _click(page, selector):
        """点元素并吞掉「找不到元素」异常，让调用方给业务化提示。"""
        try:
            page.click_selector(selector)
            return True
        except CdpError:
            return False

    def _read_refund_account(self, page, timeout=8):
        """读退款账户（原路退回落到哪张卡）的掩码，取不到返回空串。

        实测坑：该区块是跳到成功页之后约 2 秒才渲染出来的，落地瞬间读会是空；
        且账户明细接口返回空时平台会把状态文案渲染成占位符 ``Error``（那是它自己的
        显示问题，不影响退款），故这里轮询到出值为止，识别不出就按空处理。
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            value = page.js_evaluate(_JS_REFUND_ACCOUNT) or ''
            if value:
                return value
            time.sleep(0.6)
        return ''

    def _select_refund_path(self, page):
        """把退款路径切到「原路退回」，返回 (是否成功, 失败原因)。

        实测坑：退款路径区是点完 Confirmar 之后约 6 秒才渲染出来的（前几秒
        ``.refund-path-option`` 数量就是 0），一出现就点会落在「节点在、交互还没绑」
        的中间态上——点击被吞、checked 仍为 false。因此这里不用固定 sleep 猜时间，
        改成**取点→点击→等真被选中**的重试循环，每轮重新取一次坐标（渲染期间
        布局还会变，旧坐标会错位）。
        """
        if not page.wait_for(_JS_PATH_PRESENT, timeout=25):
            return False, '退款路径区未出现'
        checked_js = _JS_PATH_CHECKED % json.dumps(REFUND_PATH_LABEL_KEY)
        for _ in range(4):
            if page.wait_for(checked_js, timeout=1):
                return True, ''
            point = page.js_evaluate(_JS_PATH_POINT % json.dumps(
                REFUND_PATH_LABEL_KEY))
            if not point:
                time.sleep(1.5)
                continue
            if point.get('checked'):
                return True, ''
            page.native_click_point(point['x'], point['y'])
            if page.wait_for(checked_js, timeout=8):
                return True, ''
            time.sleep(1.2)
        return False, '退款路径未切换到原路退回（已重试 4 次）'

    def _fail_claim(self, order_no, status, reason, page):
        with self._lock:
            row = self._claim_rows.get(order_no) or {}
            row.update({'status': status, 'errorSummary': reason})
            self._claim_rows[order_no] = row
        if page is not None:
            self._capture_screenshot(self._claim_rows, order_no, page)

    def _publish_claim(self, order_no, patch):
        with self._lock:
            row = self._claim_rows.get(order_no) or {}
            row.update(patch)
            self._claim_rows[order_no] = row

    def _publish_scan(self, serial, patch):
        with self._lock:
            row = self._scan_rows.get(serial) or {}
            row.update(patch)
            self._scan_rows[serial] = row

    # ---- 页面能力 ----

    def _open_env(self, env, serial, headless):
        """打开（或复用）环境浏览器并连上 CDP，返回 (page, 是否本模块开启)。"""
        if not env:
            raise RuntimeError(
                '未匹配到唯一环境：请用环境序号或环境 ID 定位（重名环境'
                '必须用序号）')
        container_code = str(env.get('containerCode') or '') or serial
        opened_before = container_code in self.hub.open_container_codes()
        data = self.hub.browser_start(container_code, headless=headless) or {}
        port = int(data.get('debuggingPort') or 0)
        if not port:
            raise RuntimeError('start-browser 未返回调试端口')
        return self._attach_buyer_page(port), (not opened_before)

    def _attach_buyer_page(self, port):
        cdp = CdpClient(port)
        for info in cdp.list_pages():
            url = info.get('url') or ''
            if 'shein.com' in url and 'chrome-extension' not in url:
                page = cdp.attach_page(target_id=info.get('id'))
                page.bring_to_front()
                return page
        page = cdp.new_page()
        page.goto(ORIGIN + '/', settle_seconds=5.0)
        return page

    def _stop_env(self, env, serial):
        try:
            self.hub.browser_stop(
                str(env.get('containerCode') or '') or serial)
        except Exception:
            pass

    def _login_required(self, page):
        url = page.url or ''
        if '/user/auth/login' in url or '/login' in url:
            return True
        try:
            return 'Iniciar sesi' in (page.inner_text() or '')[:400]
        except CdpError:
            return False

    def _scroll_orders_list(self, page, rounds=3):
        """触底加载更多订单卡片（列表按滚动增量渲染）。"""
        for _ in range(rounds):
            page.js_evaluate(
                '(() => { window.scrollTo(0, document.body.scrollHeight);'
                ' return true; })()')
            time.sleep(1.2)

    def _pre_info(self, page, order_no):
        """在页面上下文里调买家端只读接口，判该单当前可退包裹。

        ``js_evaluate`` 走的是同步 Runtime.evaluate，拿不到 Promise 结果，
        因此用「先发起请求挂到 window、再轮询读取」两步法。请求由页面自身
        发起，登录 Cookie 与同源策略天然满足。
        """
        page.js_evaluate(
            '(() => { window.__xyPre=null; window.__xyPreErr="";'
            ' fetch("/bff-api/trade-api/refund_only/pre_info'
            '?_ver=1.1.8&_lang=es", {method:"POST", credentials:"include",'
            ' headers:{"Content-Type":"application/json"},'
            ' body: JSON.stringify({billno: %s})})'
            '.then(r=>r.json()).then(j=>{window.__xyPre=j;})'
            '.catch(e=>{window.__xyPreErr=String((e&&e.message)||e);});'
            ' return true; })()' % json.dumps(str(order_no)))
        page.wait_for('(() => !!window.__xyPre || !!window.__xyPreErr)()',
                      timeout=20)
        data = page.js_evaluate(
            '(() => { const j=window.__xyPre; if(!j) return null;'
            ' const pm=(j.info||{}).package_module||{};'
            ' const rm=(j.info||{}).reason_module||{};'
            ' return {code:String(j.code||""), msg:String(j.msg||""),'
            ' reasonId: rm.reason_id||"",'
            ' eligible:(pm.package_list||[]).map(p=>({packageNo:'
            'String(p.package_no||""), shippingNo:String(p.shipping_no||""),'
            ' title:String(p.title||""),'
            ' goodsImg:String(((p.item_list||[])[0]||{}).goods_img||"")})),'
            ' blocked:(pm.disable_package_list||[]).map(p=>'
            'String(p.package_no||""))}; })()') or {}
        if not data:
            error = page.js_evaluate('String(window.__xyPreErr||"")') or ''
            raise RuntimeError('可售后性查询失败：%s' % (error or '无响应'))
        return data

    def _capture_screenshot(self, rows, key, page):
        if page is None:
            return
        try:
            content, _, _ = page.capture_element_union(['body'])
        except Exception:
            return
        digest = hashlib.sha256(content).hexdigest()
        with self._lock:
            self._screenshots[str(key)] = content
            row = rows.get(str(key))
            if row:
                row['screenshotSha256'] = digest
                row['screenshotStatus'] = 'ok'

    @staticmethod
    def _account_name(env):
        accounts = env.get('accounts') or []
        if accounts and isinstance(accounts[0], dict):
            return str(accounts[0].get('accountName') or '').strip()[:64]
        return ''
