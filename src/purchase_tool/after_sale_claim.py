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
from concurrent.futures import ThreadPoolExecutor

import hashlib
import json
import random
import re
import threading
import time
import uuid
from datetime import datetime, timezone

from .cdp import CdpClient, CdpError
from .redaction import scrub_text
from .after_sale_display import order_item_summary, delivery_summary

ORIGIN = 'https://www.shein.com.mx'
# 从「所有订单」开始：运输中、已送达和退款单可能落在不同分类。
# 订单是否存在与是否可以申请丢件退款是两件事，不能只保留有入口的卡片。
ORDERS_STATUS_TYPE = 0
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
    r'([0-9]{1,2}\s*[A-Za-zÀ-ÿ]{3,15}\.?\s*[0-9]{4}(?:\s*[0-9:]{4,8})?)', re.I)
AMOUNT_RE = re.compile(r'\$MXN\s*([\d,]+\.?\d*)')
REFUND_DONE_RE = re.compile(
    r'Procesamiento de reembolsos|En revisi[óo]n vendedor', re.I)
# 已退款（含已处理、银行处理中）：这类单不需要也不允许再申请售后
REFUNDED_RE = re.compile(
    r'Reembolsos procesados|Reembolsado|reembolso est[áa] siendo procesado', re.I)
MAX_ORDER_LIST_PAGES = 100

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
  const out=[]; const cards=[...document.querySelectorAll(".j-order-list li.list-item")]
    .filter(n=>n.offsetWidth || n.offsetHeight);
  for (const li of cards) {
    const t = li.innerText || "";
    const entry = [...li.querySelectorAll("a")].some(a =>
      norm(a.innerText).indexOf(%s) >= 0);
    const status = li.querySelector('.order-status-text .status-text');
    const img = li.querySelector('img.crop-image-container__img');
    const track = li.querySelector('.order-list-track__title');
    const when = li.querySelector('.order-list-track__time');
    const delivered = /^Entregado(?:\\s|$)/i.test((track ? track.innerText : '').trim());
    out.push({text: t.slice(0, 2400), hasEntry: entry,
      statusText: status ? status.innerText : '',
      statusDetail: [...li.querySelectorAll('.status-ctn_text')].map(n => n.innerText.trim()).filter(Boolean).join(' / '),
      itemCount: (() => { const m=t.match(/(?:^|\\n)\\s*(\\d+)\\s+Art[ií]culos?\\b/i); return m ? Number(m[1]) : null; })(),
      goodsImages: [...li.querySelectorAll('img.crop-image-container__img')].map(i=>i.getAttribute('src') || '').filter(Boolean),
      goodsItems: [...li.querySelectorAll('img.crop-image-container__img')].map(i=>({
        goodsImg:i.getAttribute('src') || '', name:i.getAttribute('alt') || '', quantity:null, specification:''})),
      delivered: delivered,
      deliveredAt: delivered && when ? when.innerText.trim() : '',
      goodsImg: img ? (img.getAttribute('src') || '') : ''});
  }
  return out; })()''')

# 只认可订单列表自身的空态，购物袋的 empty 不能作为「无订单」证据。
# 等待卡片/明确空态及 loading 消失；超时应报查询失败，不能猜成无订单。
_JS_ORDER_LIST_STATE = r'''(() => {
  const visible = e => !!e && !!(e.offsetWidth || e.offsetHeight);
  const root = document.querySelector('.j-order-list');
  if (!root) return {ready:false};
  const selected = root.querySelector('[role=tab][aria-selected=true] [data-id]');
  const all = !!selected && selected.getAttribute('data-id') === '0';
  const loading = [...root.querySelectorAll('.order-list-loading')].some(visible);
  const cards = [...root.querySelectorAll('li.list-item')].filter(visible);
  const empty = [...root.querySelectorAll('.c-order-search')].some(e =>
    visible(e) && /Se encuentra vac[ií]o/i.test(e.innerText || ''));
  const next = root.querySelector('.sui-pagination__next');
  const nextEnabled = visible(next) && !next.disabled &&
    next.getAttribute('aria-disabled') !== 'true' &&
    !next.classList.contains('sui-pagination__btn-disabled');
  const orders = cards.map(e =>
    (String(e.innerText || '').match(/N[úu]m\.?\s*de\s*pedido\s*([A-Z0-9]{6,})/i) || [])[1] || '');
  return {ready:all && !loading && (cards.length > 0 || empty),
    empty:empty && !cards.length, next:nextEnabled, signature:orders.join('|')};
})()'''

# 扫描专用只读请求：每次使用独立状态对象，超时旧响应不能覆盖下一单。
# 只投影所需字段，不把完整平台响应或账户资料带回执行器。
_JS_SCAN_PRE_INFO = r'''(() => {
  const input = %s;
  const state = {id:input.id, done:false, error:'', response:null,
    controller:new AbortController()};
  window.__xyScanPre = state;
  if (location.origin !== input.origin) {
    state.error='page_changed'; state.done=true; return false;
  }
  fetch('/bff-api/trade-api/refund_only/pre_info?_ver=1.1.8&_lang=es', {
    method:'POST', credentials:'include', signal:state.controller.signal,
    headers:{'Content-Type':'application/json'}, body:JSON.stringify({billno:input.orderNo})
  }).then(async r => {
    const j = await r.json();
    const info = j && j.info || {};
    const pm = info.package_module || {};
    state.response = {
      httpStatus:r.status, code:j && j.code,
      reasonId:(info.reason_module || {}).reason_id,
      eligible:Array.isArray(pm.package_list) ? pm.package_list.map(p => {
        if (!p || typeof p !== 'object') return null;
        const first = Array.isArray(p.item_list) ? (p.item_list[0] || {}) : {};
        return {packageNo:p.package_no, shippingNo:String(p.shipping_no || ''),
          title:String(p.title || ''), goodsImg:String(first.goods_img || '')};
      }) : null,
      blocked:Array.isArray(pm.disable_package_list)
        ? pm.disable_package_list.map(p => p && p.package_no) : null
    };
    state.done=true;
  }).catch(() => {state.error='request_failed'; state.done=true;});
  return true;
})()'''

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

# 退款跟踪：退款单页的进度时间轴与金额。只读回访用，绝不点任何按钮。
# 阶段文案实测（真机）：受理「Solicitud de reembolso aceptada」→ 审核中
# 「Reseña de SHEIN/Vendedor」+倒计时 →「Procesamiento de reembolsos de SHEIN」；
# 终态「Reembolsos procesados / Reembolsado」（退款已处理，银行侧 5-15 工作日）。
_JS_TRACK_STATE = ('(() => {' + _JS_NORM + '''
  const cl = s => String(s||"").replace(/[\u4e00-\u9fa5]+/g," ")
    .replace(/\s+/g," ").trim();
  const body = cl(document.body ? document.body.innerText : "");
  const i = body.toLowerCase().indexOf("solicitud de reembolso");
  const timeline = i >= 0 ? body.slice(i, i + 600) : body.slice(0, 600);
  const amount = (body.match(/\\$MXN ?([\d,]+\.\d{2})/g) || []).slice(0, 3);
  const countdown = (body.match(/Termina en ([0-9:\\s]{4,12})/) || [])[1] || "";
  const acc = document.querySelector(".refundAccount-info .tip");
  const accText = acc ? String(acc.innerText||"").replace(/\s+/g," ").trim() : "";
  return { timeline: timeline, fullText: body.slice(0, 2400),
           amounts: amount, countdown: countdown.trim(),
           account: /^[*0-9\\s-]{4,24}$/.test(accText) ? accText : "" }; })()''')

# 顺序即优先级。注意：退款单页的时间轴**会把所有步骤都列出来**（含尚未到达的
# 「Procesamiento de reembolsos de SHEIN」），所以不能按「某串是否出现」判当前步骤——
# 那是踩过的坑。当前步骤的可靠信号是节点上的状态文案（审核节点带「está en revisión」
# 与 24 小时倒计时）。因此 reviewing 必须排在 processing 之前，且只认当前步骤文案。
PHASE_RULES = (
    ('refunded', ('Reembolsos procesados', 'Reembolsado',
                  'reembolso está siendo procesado')),
    ('rejected', ('rechazad', 'denegad', 'Reembolso rechazado')),
    ('reviewing', ('está en revisión', 'esta en revision', 'Termina en')),
    ('processing', ('Procesamiento de reembolsos',)),
    ('submitted', ('Solicitud de reembolso aceptada',)),
)
PHASE_LABELS = {
    'submitted': '已受理', 'reviewing': '审核中', 'processing': '处理中',
    'refunded': '已退款', 'rejected': '已拒绝', 'overdue': '超期未出结果',
    'fail': '回访失败',
}
# 平台业务终态：不代表禁止用户再次显式只读回访。
TRACK_TERMINAL_PHASES = ('refunded', 'rejected')
TRACK_OVERDUE_DAYS = 8   # 页面写明结果 7 天内给；超过即标超期


def classify_track_phase(timeline):
    """从退款单页时间轴文案判定阶段（终态优先，避免被历史文案带偏）。"""
    text = str(timeline or '')
    for phase, needles in PHASE_RULES:
        if any(n in text for n in needles):
            return phase
    return ''


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


def scan_platform_evidence(card):
    """只保留订单卡片内的平台状态，不回传整张卡片的账户/商品文字。"""
    parts = []
    for key in ('statusDetail', 'statusText'):
        text = scrub_text(str(card.get(key) or '')).strip()
        if text and text not in parts:
            parts.append(text)
    return ' / '.join(parts)[:240]


def scan_unavailable_reason(card, order):
    """具体审核节点优先于订单的笼统退款流程状态；未知原因不得猜测。"""
    status = str(card.get('statusText') or '').strip()
    detail = str(card.get('statusDetail') or '').strip()
    # 旧卡片仅有 text 时也可识别明确状态句，但不把按钮/订单导航当作状态。
    evidence = detail + '\n' + status
    raw = str(card.get('text') or '')
    review_evidence = evidence
    if not detail and not re.search(r'Reembolsado|Reembolsos procesados', status, re.I):
        review_evidence += '\n' + '\n'.join(line for line in raw.splitlines()
            if re.fullmatch(r'En revisi[óo]n (?:vendedor(?:/SHEIN)?|SHEIN)\.?', line.strip(), re.I))
    if re.search(r'En revisi[óo]n (?:vendedor(?:/SHEIN)?|SHEIN)', review_evidence, re.I):
        return 'refund_reviewing', '已有退款申请 · 卖家/SHEIN审核中；当前无丢件退款申请入口'
    if re.search(r'En revisi[óo]n|Reseña de SHEIN', evidence, re.I):
        return 'refund_reviewing', '已有退款申请 · 审核中；当前无丢件退款申请入口'
    if re.search(r'Reembolso est[áa] siendo procesado|Reembolsando', evidence, re.I):
        return 'refund_processing', '已有退款申请 · 退款处理中（不代表已到账）；当前无丢件退款申请入口'
    if re.search(r'Reembolsado', evidence, re.I):
        return 'refund_completed', '平台显示已退款；到账情况请查看退款详情'
    if re.search(r'Reembolsos procesados', evidence, re.I):
        return 'refund_processed', '平台显示退款已处理；到账情况请查看退款详情'
    if order.get('refundInProgress') or re.search(r'Procesamiento de reembolsos', evidence, re.I):
        return 'refund_in_progress', '已有退款申请 · 退款流程中，详细阶段待核对；当前无丢件退款申请入口'
    if card.get('delivered') or order.get('deliveredAt') or re.search(r'Entregado|Recibido', status, re.I):
        return 'delivered_no_entry', '已送达；未发现丢件退款入口，具体原因待核对'
    if re.search(r'Enviado', status, re.I):
        return 'in_transit', '运输中（Enviado）；当前没有丢件退款申请入口'
    if re.search(r'Procesando', status, re.I):
        return 'preparing', '备货中（Procesando）；当前没有丢件退款申请入口'
    if re.search(r'No pagado|Pendiente de pago', status, re.I):
        return 'unpaid', '待付款；当前没有丢件退款申请入口'
    if re.search(r'Cancelad', status, re.I):
        return 'cancelled', '订单已取消；当前没有丢件退款申请入口'
    return 'entry_missing', '未发现丢件退款入口；具体原因待核对'


def scan_unavailable_note(card, order):
    return scan_unavailable_reason(card, order)[1]


def validate_scan_pre_info(data):
    """只有成功、完整且理由正确的只读响应才能判定可申请/不可申请。"""
    if not isinstance(data, dict):
        raise RuntimeError('可申请性接口返回结构异常')
    http_status = data.get('httpStatus')
    if not isinstance(http_status, int) or not 200 <= http_status < 300:
        raise RuntimeError('可申请性接口 HTTP 请求失败')
    if str(data.get('code')) != '0':
        raise RuntimeError('可申请性接口未返回成功结果')
    if str(data.get('reasonId')) != '83':
        raise RuntimeError('平台未确认已送达未收到的退款理由')
    eligible, blocked = data.get('eligible'), data.get('blocked')
    if not isinstance(eligible, list) or not isinstance(blocked, list):
        raise RuntimeError('可申请性接口缺少包裹列表')
    numbers = []
    for package in eligible:
        if not isinstance(package, dict) or not isinstance(package.get('packageNo'), str) \
                or not package['packageNo'].strip():
            raise RuntimeError('可申请性接口可退包裹标识异常')
        numbers.append(package['packageNo'].strip())
    if any(not isinstance(number, str) or not number.strip() for number in blocked):
        raise RuntimeError('可申请性接口不可退包裹标识异常')
    if len(numbers) != len(set(numbers)) or set(numbers).intersection(
            number.strip() for number in blocked):
        raise RuntimeError('可申请性接口包裹状态冲突')
    return data


def refund_bill_id_from_url(url):
    """从成功页 URL 取 ``(订单号, 退款单ID)``；不在成功页返回 None。

    成功页形如：``/orders/refundLabel/<billno>?refund_bill_id_list=
    <billno>_<refund_bill_id>``。
    """
    match = REFUND_BILL_ID_RE.search(str(url or ''))
    return (match.group(1), match.group(2)) if match else None


class AfterSaleClaimer(object):
    """售后扫描与提交编排：后台线程驱动，snapshot() 供进度轮询。"""

    def __init__(self, hub, log=None, headless=True,
                 stagger_seconds=1.5, order_stagger=(5.0, 15.0)):
        self.hub = hub
        self.headless = bool(headless)
        self._concurrency = 2
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
        self._track_rows = {}
        self._screenshots = {}

    # ---- 对外入口（本地 HTTP 端点消费） ----

    def start_scan(self, serials, browser_mode=None, headless=None,
                    concurrency=2):
        """启动一批只读扫描；运行中重复发起会被拒绝。"""
        return self._start('scan', serials, browser_mode, headless, concurrency)

    def start_submit(self, items, browser_mode=None, headless=None,
                    concurrency=2):
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
        return self._start('claim', cleaned, browser_mode, headless, concurrency)

    def start_track(self, items, browser_mode=None, headless=None,
                    concurrency=2):
        """启动一批只读回访（退款跟踪）：items=[{environmentSerial, orderNo, refundBillId}]。"""
        cleaned = []
        for item in items or []:
            if not isinstance(item, dict):
                continue
            serial = str(item.get('environmentSerial') or '').strip()
            order_no = str(item.get('orderNo') or '').strip()
            bill = str(item.get('refundBillId') or '').strip()
            if not (serial and order_no and bill):
                continue
            cleaned.append({
                'environmentSerial': serial, 'orderNo': order_no,
                'refundBillId': bill,
                'storeName': str(item.get('storeName') or '').strip()[:128],
            })
        return self._start('track', cleaned, browser_mode, headless, concurrency)

    def request_stop(self):
        self._stop_event.set()
        return {'stopRequested': True}

    def snapshot(self):
        """扫描与提交两个视图合并返回，前端按 mode 分别渲染。"""
        with self._lock:
            scan_rows = [dict(r) for r in self._scan_rows.values()]
            claim_rows = [dict(r) for r in self._claim_rows.values()]
            track_rows = [dict(r) for r in self._track_rows.values()]
            running = self._running
            mode = self._mode
        scan_rows.sort(key=lambda r: (str(r.get('environmentSerial') or ''),
                                      str(r.get('orderNo') or '')))
        for row in scan_rows:
            row['screenshotStatus'] = 'ok' if row.get('screenshotSha256') \
                else ''
        # 字典按请求清单插入，保持实际执行顺序，不能再按订单号重排。
        for row in claim_rows:
            row['screenshotStatus'] = 'ok' if row.get('screenshotSha256') \
                else ''
        return {'running': running, 'mode': mode, 'rows': scan_rows,
                'claimRows': claim_rows, 'trackRows': track_rows}

    def screenshot_bytes(self, key):
        with self._lock:
            return self._screenshots.get(str(key))

    # ---- 批次编排 ----

    def _start(self, mode, items, browser_mode, headless=None, concurrency=2):
        if type(concurrency) is not int or concurrency not in (2, 3, 5):
            raise ValueError('售后环境并发数必须为 2、3、5 中 的整数')
        if browser_mode not in (None, 'headless', 'visible'):
            raise ValueError('售后浏览器模式无效')
        with self._lock:
            if self._running:
                return {'running': True, 'mode': self._mode,
                        'error': '已有售后任务运行中'}
            self._running = True
            self._concurrency = concurrency
            self._mode = mode
            self._stop_event = threading.Event()
            self._screenshots = {}
            if mode == 'scan':
                self._scan_rows = {}
                keys = [str(s) for s in items if str(s or '').strip()]
            elif mode == 'track':
                self._track_rows = {}
                keys = [item['refundBillId'] for item in items]
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
            headless = (browser_mode == 'headless' if browser_mode is not None
                        else self.headless)
            if mode == 'scan':
                self._run_scan(items, headless)
            elif mode == 'track':
                self._run_track(items, headless)
            else:
                self._run_claim(items, headless)
        except Exception as exc:  # 批次级异常也要把 running 落回 False
            self._log('售后批次异常: %s' % exc)
        finally:
            with self._lock:
                self._running = False

    def _run_environment_jobs(self, jobs, stagger=0):
        """按环境限流；序号/名称等别名解析到同一环境时也不允许同时操作。"""
        locks = {}
        for serial, env, _, _ in jobs:
            key = str(env.get('containerCode') or serial).casefold()
            locks.setdefault(key, threading.Lock())

        def execute(job):
            serial, env, callback, args = job
            key = str(env.get('containerCode') or serial).casefold()
            with locks[key]:
                if not self._stop_event.is_set():
                    callback(*args)

        with ThreadPoolExecutor(max_workers=self._concurrency) as pool:
            futures = []
            for index, job in enumerate(jobs):
                if self._stop_event.is_set():
                    break
                if index and stagger and self._stop_event.wait(stagger):
                    break
                futures.append(pool.submit(execute, job))
            for future in futures:
                future.result()

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
        self._run_environment_jobs([
            (serial, env_index.get(serial, {}), self._scan_one_guarded,
             (serial, env_index.get(serial, {}), headless))
            for serial in serials
        ], stagger=self._stagger)
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
            cards = self._read_all_order_cards(page)
            candidates = []
            for card in cards:
                parsed = parse_order_card(card.get('text') or '')
                if not parsed:
                    raise RuntimeError('订单卡片格式无法识别，请人工核对所有订单页')
                # 送达时间可能晚于订单卡片渲染；结构化时间避免长卡片截断丢值。
                delivery = DELIVERED_RE.search(
                    'Entregado a ' + str(card.get('deliveredAt') or ''))
                if delivery and not parsed['deliveredAt']:
                    parsed['deliveredAt'] = delivery.group(1).strip()
                parsed.update(order_item_summary(card))
                parsed['packages'] = []
                parsed['blockedPackages'] = []
                parsed['claimable'] = False
                parsed['status'] = 'skip'
                parsed['goodsImg'] = str(card.get('goodsImg') or '')[:300]
                parsed['reasonCode'], parsed['note'] = scan_unavailable_reason(card, parsed)
                parsed['platformStatus'] = scan_platform_evidence(card)
                parsed['reasonSource'] = 'order_list'
                parsed['checkedAt'] = datetime.now(timezone.utc).isoformat()
                if self._stop_event.is_set():
                    parsed['status'] = 'stopped'
                    parsed['note'] = '扫描已停止，尚未核验可申请性'
                    parsed['reasonCode'] = 'stopped'
                elif card.get('hasEntry'):
                    parsed['reasonSource'] = 'pre_info'
                    # 所有订单里的入口同样要体检；不因所在分类而强制跳过。
                    try:
                        info = self._scan_pre_info(page, parsed['orderNo'])
                        parsed['packages'] = info.get('eligible') or []
                        parsed['blockedPackages'] = info.get('blocked') or []
                        parsed['claimable'] = bool(parsed['packages'])
                        parsed['status'] = 'ok' if parsed['claimable'] else 'blocked'
                        parsed['reasonCode'] = '' if parsed['claimable'] else 'no_eligible_packages'
                        parsed['reasonSource'] = 'pre_info'
                        parsed['note'] = ('' if parsed['claimable'] else
                                          '平台资格核验未返回可申请的丢件退款包裹；具体限制原因待核对')
                    except Exception as exc:
                        parsed['status'] = 'fail'
                        parsed['reasonCode'] = 'eligibility_read_failed'
                        parsed['note'] = scrub_text('可申请性核验失败：%s' % exc)[:200]
                parsed.update(delivery_summary(card, parsed))
                candidates.append(parsed)
            with self._lock:
                row = self._scan_rows.get(serial) or {}
                row.update({
                    'status': ('stopped' if self._stop_event.is_set() else
                               'fail' if any(c['status'] == 'fail' for c in candidates) else
                               'ok' if any(c['claimable'] for c in candidates) else
                               'skip' if candidates else 'empty'),
                    'orders': candidates,
                    'orderNo': candidates[0]['orderNo'] if candidates else '',
                    'deliveredAt': (candidates[0]['deliveredAt']
                                    if candidates else ''),
                    'amount': candidates[0]['amount'] if candidates else '',
                    'packages': (candidates[0]['packages']
                                 if candidates else []),
                    'claimable': any(c['claimable'] for c in candidates),
                    'durationSeconds': int(time.time() - started),
                    'errorSummary': (None if candidates else
                                     '扫描已停止，未确认订单列表' if self._stop_event.is_set()
                                     else '所有订单列表为空'),
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
        self._run_environment_jobs([
            (serial, env_index.get(serial, {}), self._claim_env_guarded,
             (serial, env_index.get(serial, {}), group, headless))
            for serial, group in grouped.items()
        ])
        with self._lock:
            for item in items:
                row = self._claim_rows.get(item['orderNo'])
                if row and row.get('status') in CLAIM_RUNNING_STATES:
                    row['status'] = 'stopped'

    def _run_track(self, items, headless):
        """按环境分组限流回访；同环境单与单之间串行、轻停顿。"""
        env_index = self._env_index([i['environmentSerial'] for i in items])
        with self._lock:
            for item in items:
                self._track_rows[item['refundBillId']] = {
                    'refundBillId': item['refundBillId'],
                    'orderNo': item['orderNo'],
                    'environmentSerial': item['environmentSerial'],
                    'storeName': item.get('storeName')
                    or (env_index.get(item['environmentSerial'], {})
                        .get('containerName')) or '',
                    'status': 'queued', 'phase': '', 'phaseLabel': '',
                    'timeline': '', 'countdown': '', 'refundAccount': '',
                    'amount': '', 'checkedAt': '', 'note': '',
                    'errorSummary': None, 'durationSeconds': None,
                }
        grouped = {}
        for item in items:
            grouped.setdefault(item['environmentSerial'], []).append(item)
        self._run_environment_jobs([
            (serial, env_index.get(serial, {}), self._track_env_guarded,
             (serial, env_index.get(serial, {}), group, headless))
            for serial, group in grouped.items()
        ])
        with self._lock:
            for item in items:
                row = self._track_rows.get(item['refundBillId'])
                if row and row.get('status') in ('queued', 'running'):
                    row['status'] = 'stopped'

    def _track_env_guarded(self, serial, env, items, headless):
        try:
            self._track_env(serial, env, items, headless)
        except Exception as exc:
            for item in items:
                self._fail_track(item['refundBillId'], 'fail',
                                 scrub_text(str(exc))[:200])

    def _track_env(self, serial, env, items, headless):
        page = None
        opened_by_me = False
        try:
            page, opened_by_me = self._open_env(env, serial, headless)
            for index, item in enumerate(items):
                if self._stop_event.is_set():
                    return
                if index:
                    time.sleep(random.uniform(2.0, 5.0))  # 只读，轻停顿即可
                try:
                    self._track_one(page, item)
                except Exception as exc:
                    self._fail_track(
                        item['refundBillId'], 'fail',
                        scrub_text('%s: %s' % (type(exc).__name__,
                                               str(exc)))[:200])
        finally:
            if opened_by_me:
                self._stop_env(env, serial)

    def _track_one(self, page, item):
        """回访单个退款单：读进度时间轴判阶段 + 读退款账户。只读，不点任何按钮。"""
        started = time.time()
        bill = item['refundBillId']
        self._publish_track(bill, {'status': 'running'})
        url = ('%s/orders/refundLabel/%s?refund_bill_id_list=%s_%s'
               % (ORIGIN, item['orderNo'], item['orderNo'], bill))
        page.goto(url, dom_timeout=45, settle_seconds=4.0)
        if self._login_required(page):
            self._fail_track(bill, 'login', '买家端未登录（环境登录态缺失）')
            return
        # 时间轴异步渲染：轮询到出文案再判阶段（这个后台渲染时间不稳定，别估 sleep）
        state = {}
        deadline = time.time() + 15
        while time.time() < deadline:
            state = page.js_evaluate(_JS_TRACK_STATE) or {}
            if state.get('timeline'):
                break
            time.sleep(0.8)
        timeline = state.get('timeline') or ''
        phase = classify_track_phase(timeline)
        if phase in ('', 'submitted') and state.get('fullText'):
            # 切片可能落在导航栏（页面标题大小写与预期不符时），用全文兜底再判一次
            phase = classify_track_phase(state['fullText'])
        if not phase:
            self._fail_track(bill, 'fail', '未读到可识别的退款阶段，请稍后重试')
            return
        account = state.get('account') or ''
        if not account:
            account = self._read_refund_account(page, timeout=6)
        note = ''
        if phase == 'rejected':
            note = '需人工：到买家端看 Historial de negociación 的拒绝理由后决定是否申诉'
        elif phase not in TRACK_TERMINAL_PHASES:
            note = '未终态，下次回访继续跟'
        amounts = state.get('amounts') or []
        with self._lock:
            row = self._track_rows.get(bill) or {}
            row.update({
                'status': 'ok', 'phase': phase,
                'phaseLabel': PHASE_LABELS.get(phase, phase),
                'timeline': timeline[:400],
                'countdown': state.get('countdown') or '',
                'refundAccount': account,
                'amount': (amounts[0] if amounts else '').replace('$MXN', '').strip(),
                'checkedAt': datetime.now(timezone.utc).isoformat(),
                'note': note, 'errorSummary': None,
                'durationSeconds': int(time.time() - started),
            })
            self._track_rows[bill] = row

    def _fail_track(self, bill, status, reason):
        with self._lock:
            row = self._track_rows.get(bill) or {}
            row.update({'status': status, 'errorSummary': reason})
            self._track_rows[bill] = row

    def _publish_track(self, bill, patch):
        with self._lock:
            row = self._track_rows.get(bill) or {}
            row.update(patch)
            self._track_rows[bill] = row

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
            if self._stop_event.is_set():
                self._publish_claim(order_no, {'status': 'stopped'})
                return
            guard += 1
            ok = self._submit_package(page, order_no)
            if not ok.get('ok'):
                self._fail_claim(order_no, 'fail', ok.get('reason') or '提交失败',
                                 page)
                return
            self._record_refund(order_no, ok)
            submitted.append(ok)
            if ok.get('verificationError'):
                self._fail_claim(order_no, 'fail', ok['verificationError'], page)
                return
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
                'status': 'fail' if leftover else 'ok',
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
        if not bill or not bill[1]:
            return {'ok': False, 'reason': '已跳转退款页但未取得退款单号，请人工核对',
                    'packageNo': package_no}
        result = {'ok': True, 'packageNo': package_no, 'refundBillId': bill[1],
                  'refundAccount': ''}
        # 一旦平台生成退款单号就记录，后续读账户或资格失败不能丢失已受理凭证。
        self._record_refund(order_no, result)
        try:
            result['remaining'] = self._pre_info(page, order_no).get('eligible') or []
            result['refundAccount'] = self._read_refund_account(page)
        except Exception as exc:
            result['verificationError'] = '已受理，但后续核验失败：' + scrub_text(str(exc))[:140]
        self._record_refund(order_no, result)
        return result

    def _record_refund(self, order_no, result):
        bill = str(result.get('refundBillId') or '')
        if not bill:
            return
        with self._lock:
            row = self._claim_rows.setdefault(order_no, {'orderNo': order_no})
            refunds = {r['refundBillId']: dict(r) for r in row.get('refunds', [])}
            entry = refunds.get(bill, {})
            entry.update({
                'refundBillId': bill, 'packageNo': result.get('packageNo') or '',
                'refundPath': REFUND_PATH_LABEL,
                'refundAccount': result.get('refundAccount') or entry.get('refundAccount', ''),
                'submittedAt': entry.get('submittedAt') or datetime.now(timezone.utc).isoformat(),
            })
            refunds[bill] = entry
            row['refunds'] = list(refunds.values())
            row.update(entry)
            row['packageCount'] = len(refunds)

    @staticmethod
    def _click(page, selector):
        """点元素并吞掉「找不到元素」异常，让调用方给业务化提示。"""
        try:
            page.click_selector(selector)
            return True
        except CdpError:
            return False

    def _read_refund_account(self, page, timeout=15):
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

    def _read_all_order_cards(self, page):
        """读取所有订单的分页，去重；加载/翻页失败不伪装成空订单。"""
        cards = {}
        previous_signature = None
        for _ in range(MAX_ORDER_LIST_PAGES):
            if self._stop_event.is_set():
                return list(cards.values())
            changed = ('' if previous_signature is None else
                       ' && s.signature !== %s' % json.dumps(previous_signature))
            ready = ('(() => { const s=%s; return s.ready%s; })()'
                     % (_JS_ORDER_LIST_STATE, changed))
            if not page.wait_for(ready, timeout=30):
                raise RuntimeError('所有订单列表未就绪或翻页未完成，请重试扫描')
            self._scroll_orders_list(page)
            if not page.wait_for(ready, timeout=15):
                raise RuntimeError('所有订单列表仍在加载，请重试扫描')
            # 已观察到：入口已渲染，但送达日期稍后才补上。有限等待展示字段，
            # 日期始终缺失也不据此排除有入口的订单，可申请性仍以接口为准。
            scan_js = _JS_SCAN_ORDERS % json.dumps(ORDER_ENTRY_KEY)
            page.wait_for('(() => { const rows=%s; return rows.every('
                          'r => !(r.delivered || r.hasEntry) || !!r.deliveredAt); })()'
                          % scan_js, timeout=12)
            state = page.js_evaluate(_JS_ORDER_LIST_STATE) or {}
            raw_cards = page.js_evaluate(scan_js)
            if isinstance(raw_cards, list) and any(r.get('delivered') and not r.get('deliveredAt') for r in raw_cards):
                # One bounded re-read; a timeout is recorded as missing rather than fabricated.
                page.wait_for('(() => { const rows=%s; return rows.every('
                              'r => !r.delivered || !!r.deliveredAt); })()' % scan_js, timeout=4)
                retry_cards = page.js_evaluate(scan_js)
                if isinstance(retry_cards, list):
                    raw_cards = retry_cards
                state = page.js_evaluate(_JS_ORDER_LIST_STATE) or {}
            if not state.get('ready') or not isinstance(raw_cards, list):
                raise RuntimeError('所有订单列表读取失败，请重试扫描')
            if not raw_cards and not state.get('empty'):
                raise RuntimeError('未读到订单卡片，也未确认空列表，请重试扫描')
            for card in raw_cards:
                parsed = parse_order_card(card.get('text') or '') if isinstance(card, dict) else None
                if not parsed:
                    raise RuntimeError('订单卡片格式无法识别，请人工核对所有订单页')
                previous = cards.get(parsed['orderNo']) or {}
                # 跨页重复卡片的简版不能抹掉已发现的入口。仍会现场 pre_info
                # 核验，不因旧卡片曾有入口就认定可申请。
                cards[parsed['orderNo']] = dict(
                    card, hasEntry=bool(card.get('hasEntry') or previous.get('hasEntry')),
                    delivered=bool(card.get('delivered') or previous.get('delivered')),
                    deliveredAt=card.get('deliveredAt') or previous.get('deliveredAt') or '',
                    itemCount=card.get('itemCount') if card.get('itemCount') is not None else previous.get('itemCount'),
                    goodsImages=card.get('goodsImages') or previous.get('goodsImages') or [],
                    goodsItems=card.get('goodsItems') or previous.get('goodsItems') or [],
                    statusDetail=card.get('statusDetail') or previous.get('statusDetail') or '',
                    goodsImg=card.get('goodsImg') or previous.get('goodsImg') or '')
            if not state.get('next'):
                return list(cards.values())
            previous_signature = state.get('signature')
            if not self._click(page, '.j-order-list .sui-pagination__next'):
                raise RuntimeError('所有订单列表翻页失败，请重试扫描')
        raise RuntimeError('所有订单页数超出扫描上限，请人工核对，未确认扫描完整')

    def _scan_pre_info(self, page, order_no):
        """扫描与提交共用的只读资格核验。"""
        request_id = uuid.uuid4().hex
        request_json = json.dumps({'id': request_id, 'origin': ORIGIN,
                                   'orderNo': str(order_no)})
        result_js = ('(() => { const s=window.__xyScanPre; '
                     'return s && s.id === %s && s.done '
                     '? {error:s.error, response:s.response} : null; })()'
                     % json.dumps(request_id))
        try:
            page.js_evaluate(_JS_SCAN_PRE_INFO % request_json)
            if not page.wait_for(result_js, timeout=20):
                raise RuntimeError('可申请性查询超时，请重试扫描')
            result = page.js_evaluate(result_js)
            if not isinstance(result, dict) or result.get('error'):
                raise RuntimeError('可申请性查询失败或订单页面已切换，请重试扫描')
            return validate_scan_pre_info(result.get('response'))
        finally:
            try:
                page.js_evaluate(
                    '(() => { const s=window.__xyScanPre; if(s && s.id === %s) {'
                    's.controller.abort(); delete window.__xyScanPre;} })()'
                    % json.dumps(request_id))
            except Exception:
                pass

    def _pre_info(self, page, order_no):
        """提交前与提交后的只读核验复用严格响应校验。"""
        return self._scan_pre_info(page, order_no)

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
