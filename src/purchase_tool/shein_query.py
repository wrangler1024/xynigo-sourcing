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
import unicodedata
from datetime import datetime, timedelta, timezone

from .cdp import CdpClient
from .hub_api import HubApiError

SUPPORTED_SITES = ('MX', 'US')
SUPPORTED_BROWSER_MODES = ('headless', 'visible')
SITE_LABELS = {'MX': '墨西哥站', 'US': '美国站'}
SYSTEMIC_HUB_FAILURE_CODES = {
    'hubstudio_client_not_running',
    'hubstudio_local_api_disabled',
    'hubstudio_local_api_authentication_required',
    'hubstudio_local_api_authentication_failed',
    'hubstudio_local_api_incompatible',
    'hubstudio_browser_core_missing',
    'hubstudio_browser_launch_invalid',
}
# HubStudio 的 Local API 在连续启动/关闭大量环境时可能短暂重启监听端口。
# 现场批次表明资源不足后约 2-3 分钟仍可能恢复，因此不能再用原先约
# 30 秒的窗口直接终止整批。恢复等待发生在 browser/start 串行锁内、
# 控制 RPC 锁外，
# 让已经运行的环境仍能完成 browser/stop 并释放资源。
HUB_TRANSPORT_RECOVERY_DELAYS = {
    'hubstudio_local_api_unreachable': (2.0, 4.0, 8.0, 15.0,
                                         30.0, 30.0, 30.0),
    'hubstudio_local_api_timeout': (2.0, 4.0, 8.0, 15.0, 30.0),
}
HUB_RESOURCE_RECOVERY_DELAYS = (5.0, 10.0, 15.0, 30.0, 30.0, 30.0)
BROWSER_CLOSE_CONFIRM_SECONDS = 30.0
BROWSER_CLOSE_POLL_SECONDS = 1.0
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
    'Esperando para enviarse': '待发货',
    'Reembolsando': '砍单退款中', 'Reembolsado': '退款已处理',
    'Devolución': '退换货', 'No pagado': '未支付',
    'Pagado': '已支付/待备货',
    'Processing': '备货中', 'Packing': '打包中', 'Shipped': '已发货',
    'Delivered': '已送达', 'Completed': '已完成', 'Canceled': '已取消',
    'Cancelled': '已取消', 'Refunding': '退款中', 'Refunded': '已退款',
    'Returned': '已退货', 'Unpaid': '未支付', 'Paid': '已支付/待备货',
    'Risk verification': '风险订单/待验证',
}
STATUS_ALIASES = {
    # 2026-09-04 真实 MX 页面使用了 envisarse 拼写，统一成正确展示文案。
    'esperando para envisarse': 'Esperando para enviarse',
    'esperando para enviarse': 'Esperando para enviarse',
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
        r'Reembolsando|Reembolsado|Reembolsos procesados'
        r'|reembolso est[áa] siendo procesado', re.I),
    # 避免把美国详情页固定导航项 "Refund" 误判成砍单。
    'US': re.compile(
        r'\bRefunding\b|\bRefunded\b|refund (?:is )?being processed'
        r'|refund in progress', re.I),
}
# 大小写不敏感：页面出现过 EMPACANDO 大写形式（2026-08-18 实测 1007）
RE_STATUS_WORDS_BY_SITE = {
    'MX': re.compile(
        r'(Esperando para (?:enviarse|envisarse)|Procesando|Empacando'
        r'|Enviado|Reembolsando|Reembolsado|Completado|Cancelado'
        r'|Entregado|Devoluci[óo]n|No pagado|Pagado)', re.I),
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
RE_RISK_VERIFY_TEXT_BY_SITE = {
    'MX': re.compile(
        r'pedido fue detectado de estar en riesgo y requiere ser verificado'
        r'|brinda los documentos de respaldo seg[uú]n las instrucciones'
        r'|l[ií]mite de tiempo de validaci[oó]n[^.\n]*pulsa para validar',
        re.I),
    'US': re.compile(
        r'order is detected to be at risk and needs to be verified'
        r'|provide the supporting documents according to the instructions',
        re.I),
}
RE_RISK_VERIFY_FLAG = re.compile(r'"is_verify"\s*:\s*"?1"?', re.I)
RE_RISK_NOT_SUBMITTED = re.compile(
    r'"sensitive_status"\s*:\s*"no_submit"', re.I)
# Chrome 网络错误页（如 ERR_CONNECTION_CLOSED，711 代理偶发波动）
RE_ERR_PAGE = re.compile(r'(ERR_[A-Z_]+)')
# Cloudflare 安全校验页（2026-08-18 实测 1007：验证会自动通过但需额外等待）
RE_CF = re.compile(
    r'Verificaci[óo]n de seguridad|Checking your browser'
    r'|Attention Required|Verifica que (eres|no eres)', re.I)
TRACKING_MONTH_NUMBERS = {
    'jan': 1, 'january': 1, 'ene': 1, 'enero': 1,
    'feb': 2, 'february': 2, 'febrero': 2,
    'mar': 3, 'march': 3, 'marzo': 3,
    'apr': 4, 'april': 4, 'abr': 4, 'abril': 4,
    'may': 5, 'mayo': 5,
    'jun': 6, 'june': 6, 'junio': 6,
    'jul': 7, 'july': 7, 'julio': 7,
    'aug': 8, 'august': 8, 'ago': 8, 'agosto': 8,
    'sep': 9, 'sept': 9, 'september': 9, 'septiembre': 9,
    'oct': 10, 'october': 10, 'octubre': 10,
    'nov': 11, 'november': 11, 'noviembre': 11,
    'dec': 12, 'december': 12, 'dic': 12, 'diciembre': 12,
}
_TRACK_TIME = (
    r'(?P<hour>[01]?\d|2[0-3]):(?P<minute>[0-5]\d)'
    r'(?::(?P<second>[0-5]\d))?\s*(?P<ampm>AM|PM)?')
RE_TRACKING_ISO_TIME = re.compile(
    r'(?P<year>20\d{2})[-/.](?P<month>\d{1,2})[-/.]'
    r'(?P<day>\d{1,2})[ T,\s]+' + _TRACK_TIME, re.I)
RE_TRACKING_DAY_MONTH_TIME = re.compile(
    r'(?P<day>\d{1,2})\s+(?P<monthName>[A-Za-zÁÉÍÓÚÜÑáéíóúüñ.]+)'
    r'\s+(?P<year>20\d{2})[,\s]+' + _TRACK_TIME, re.I)
RE_TRACKING_MONTH_DAY_TIME = re.compile(
    r'(?P<monthName>[A-Za-zÁÉÍÓÚÜÑáéíóúüñ.]+)\s+'
    r'(?P<day>\d{1,2}),?\s+(?P<year>20\d{2})[,\s]+' + _TRACK_TIME,
    re.I)
RE_TRACKING_NUMERIC_TIME = re.compile(
    r'(?P<first>\d{1,2})[-/.](?P<secondPart>\d{1,2})[-/.]'
    r'(?P<year>20\d{2})[ T,\s]+' + _TRACK_TIME, re.I)
# SHEIN 当前轨迹组件只渲染 ``Sep 03`` + ``19:23``，年份需从订单
# 时间推断。DOM innerText 会保留换行，解析器拼接相邻行后再匹配。
RE_TRACKING_DAY_MONTH_TIME_SHORT = re.compile(
    r'(?P<day>\d{1,2})\s+(?P<monthName>[A-Za-zÁÉÍÓÚÜÑáéíóúüñ.]+)'
    r'[,\s]+' + _TRACK_TIME, re.I)
RE_TRACKING_MONTH_DAY_TIME_SHORT = re.compile(
    r'(?P<monthName>[A-Za-zÁÉÍÓÚÜÑáéíóúüñ.]+)\s+'
    r'(?P<day>\d{1,2})[,\s]+' + _TRACK_TIME, re.I)


def normalize_site(site):
    value = str(site or 'MX').strip().upper()
    if value not in SUPPORTED_SITES:
        raise ValueError('查询站点仅支持 MX（墨西哥）或 US（美国）')
    return value


def normalize_browser_mode(mode):
    value = str(mode or 'headless').strip().casefold()
    if value not in SUPPORTED_BROWSER_MODES:
        raise ValueError('物流查询浏览器模式仅支持 headless 或 visible')
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


def _tracking_month_number(raw):
    value = unicodedata.normalize('NFKD', str(raw or ''))
    value = ''.join(ch for ch in value if not unicodedata.combining(ch))
    return TRACKING_MONTH_NUMBERS.get(value.strip(' .').casefold())


def _tracking_match_datetime(match, site='MX', utc_offset_minutes=None,
                             ordered_at=None):
    """Convert one rendered carrier timestamp to the environment timezone."""
    try:
        site = normalize_site(site)
        groups = match.groupdict()
        month = groups.get('month')
        if not month and groups.get('monthName'):
            month = _tracking_month_number(groups['monthName'])
        if not month and groups.get('first'):
            if site == 'US':
                month, day = groups['first'], groups['secondPart']
            else:
                day, month = groups['first'], groups['secondPart']
        else:
            day = groups.get('day')
        hour = int(groups.get('hour') or 0)
        ampm = str(groups.get('ampm') or '').upper()
        if ampm:
            hour = hour % 12 + (12 if ampm == 'PM' else 0)
        offset = SITE_PROFILES[site]['utcOffsetMinutes'] \
            if utc_offset_minutes is None else int(utc_offset_minutes)
        tz = timezone(timedelta(minutes=offset))
        year = groups.get('year')
        if year:
            return datetime(
                int(year), int(month), int(day), hour,
                int(groups.get('minute') or 0),
                int(groups.get('second') or 0), tzinfo=tz)

        reference = ordered_at or datetime.now(tz)
        candidates = []
        for candidate_year in (
                reference.year, reference.year + 1, reference.year - 1):
            try:
                candidates.append(datetime(
                    candidate_year, int(month), int(day), hour,
                    int(groups.get('minute') or 0),
                    int(groups.get('second') or 0), tzinfo=tz))
            except ValueError:
                continue
        if ordered_at is not None:
            forward = [candidate for candidate in candidates
                       if timedelta(0) <= candidate - ordered_at
                       <= timedelta(days=366)]
            if forward:
                return min(forward)
        return min(candidates, key=lambda candidate: abs(candidate - reference))
    except (TypeError, ValueError, OverflowError):
        return None


def parse_first_tracking_event(text, order_time='', site='MX',
                               utc_offset_minutes=None):
    """Parse the earliest rendered tracking node and its order-to-track lead.

    SHEIN and downstream carriers do not expose one stable JSON contract for
    the rendered timeline.  We therefore accept the verified MX/US locale
    date families, inspect short adjacent-line windows, and choose the
    chronological minimum rather than the first DOM row (the UI is usually
    newest-first).
    """
    empty = {
        'firstTrackingAt': '', 'firstTrackingTime': '',
        'firstTrackingSummary': '', 'firstTrackingLeadMinutes': None,
    }
    site = normalize_site(site)
    offset = SITE_PROFILES[site]['utcOffsetMinutes'] \
        if utc_offset_minutes is None else int(utc_offset_minutes)
    ordered = None
    try:
        ordered = datetime.strptime(
            str(order_time or '').strip(), '%Y-%m-%d %H:%M:%S')
        ordered = ordered.replace(
            tzinfo=timezone(timedelta(minutes=offset)))
    except (TypeError, ValueError):
        pass
    lines = [re.sub(r'\s+', ' ', line).strip()
             for line in str(text or '').splitlines() if line.strip()]
    patterns = (
        RE_TRACKING_ISO_TIME, RE_TRACKING_DAY_MONTH_TIME,
        RE_TRACKING_MONTH_DAY_TIME, RE_TRACKING_NUMERIC_TIME,
        RE_TRACKING_DAY_MONTH_TIME_SHORT,
        RE_TRACKING_MONTH_DAY_TIME_SHORT,
    )
    events = {}
    for index in range(len(lines)):
        candidate = ' '.join(lines[index:index + 3])
        for pattern in patterns:
            for match in pattern.finditer(candidate):
                observed = _tracking_match_datetime(
                    match, site, utc_offset_minutes, ordered)
                if observed is None:
                    continue
                summary = candidate[match.end():].strip(' -–—|·,;')
                for next_pattern in patterns:
                    next_match = next_pattern.search(summary)
                    if next_match:
                        summary = summary[:next_match.start()].strip(
                            ' -–—|·,;')
                key = observed.isoformat()
                if key not in events or len(summary) > len(events[key][1]):
                    events[key] = (observed, summary[:300])
    if not events:
        return empty
    observed, summary = min(events.values(), key=lambda item: item[0])
    lead_minutes = None
    if ordered is not None:
        candidate_lead = int((observed - ordered).total_seconds() // 60)
        if 0 <= candidate_lead <= 366 * 24 * 60:
            lead_minutes = candidate_lead
    return {
        'firstTrackingAt': observed.isoformat(),
        'firstTrackingTime': observed.strftime('%Y-%m-%d %H:%M:%S'),
        'firstTrackingSummary': summary,
        'firstTrackingLeadMinutes': lead_minutes,
    }


def _canonical_status(word):
    wanted = (word or '').casefold()
    if wanted in STATUS_ALIASES:
        return STATUS_ALIASES[wanted]
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
        RE_RISK_VERIFY_TEXT_BY_SITE[site].search(text or '') or
        (RE_RISK_VERIFY_FLAG.search(html or '') and
         RE_RISK_NOT_SUBMITTED.search(html or '')))
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


def merge_order_status_signals(info, detail, site='MX'):
    """合并列表与详情状态，并应用退款/风险状态优先级。"""
    site = normalize_site(site)
    info = dict(info or {})
    detail = dict(detail or {})
    refund_statuses = (
        {'Reembolsando', 'Reembolsado'}
        if site == 'MX' else {'Refunding', 'Refunded'})
    if detail.get('kanDan') and info.get('status') not in refund_statuses:
        forced = 'Reembolsando' if site == 'MX' else 'Refunding'
        info.update(status=forced, statusCn=STATUS_CN[forced])
    if info.get('status') in refund_statuses:
        detail['kanDan'] = True
    if detail.get('riskOrder') and not detail.get('kanDan'):
        if info.get('status') in {'Pagado', 'Paid'}:
            info['statusCn'] = '已支付/待验证'
        else:
            info.update(
                status='Risk verification',
                statusCn=STATUS_CN['Risk verification'])
    return info, detail


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
        self.lock = threading.RLock()
        self._browser_state = threading.Condition(self.lock)
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
        self.browser_mode = 'headless'
        self.allow_open_environment = False
        # 并发 worker 的 browser/start 阶段全局串行：HubStudio 对同一/多个
        # 同时到达的 start 处理有先后，串行启动可避免 -10005 冲突
        self._start_lock = threading.Lock()
        self._active_browser_codes = set()
        self._pending_close_codes = set()
        self._resource_constrained = False
        self._runtime_state = 'idle'
        self._runtime_message = ''
        self._recovery_attempt = 0
        self._recovery_count = 0
        self._preflight_completed = False
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
                'firstTrackingAt': '', 'firstTrackingTime': '',
                'firstTrackingSummary': '',
                'firstTrackingLeadMinutes': None,
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
                'browserMode': self.browser_mode,
                'allowOpenEnvironment': self.allow_open_environment,
                'fatalError': self.fatal_error,
                'fatalErrorCode': self.fatal_error_code,
                'runtimeState': self._runtime_state,
                'runtimeMessage': self._runtime_message,
                'resourceConstrained': self._resource_constrained,
                'effectiveConcurrency': (
                    1 if (self.browser_mode == 'visible'
                          or self._resource_constrained)
                    else self.concurrency),
                'recoveryAttempt': self._recovery_attempt,
                'recoveryCount': self._recovery_count,
                'pendingCloseCount': len(self._pending_close_codes),
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

    def _capture_tracking(self, page, serial, order_no, tracks, site='MX',
                          order_time='', utc_offset_minutes=None):
        """打开 SHEIN 轨迹页并截取无地址/商品的物流区域。"""
        profile = _site_profile(site)
        page.goto(profile['orderTrackUrl'] % order_no,
                  settle_seconds=min(4.0, self.settle_seconds))
        if 'login' in page.url:
            raise RuntimeError('轨迹页登录失效')
        if not page.wait_selector('.track-steps-content', timeout=25):
            raise RuntimeError('轨迹页未加载出物流节点')
        track_html = page.outer_html()
        track_text = page.element_inner_text('.track-steps-content')
        tracking_result = parse_first_tracking_event(
            track_text, order_time, site, utc_offset_minutes)
        carrier_match = RE_CARRIER_NAME.search(track_html)
        try:
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
        except Exception as exc:
            result = {
                'screenshotState': 'fail', 'screenshotFile': '',
                'screenshotError': str(exc)[:100], 'screenshotSizeKb': 0,
                'screenshotWidth': 0, 'screenshotHeight': 0,
            }
        result.update(tracking_result)
        if carrier_match:
            result['carrier'] = carrier_match.group(1)
        return result

    # ---- 查询入口 ----

    def preflight_batch(self, serials, env_index=None, site='MX',
                        browser_mode='headless',
                        allow_open_environment=False):
        """Prove one eligible environment can launch before accepting a batch.

        A healthy ``group/list`` response does not prove that HubStudio has a
        usable browser core.  This reversible start/stop probe keeps a broken
        installation from creating a formal run and then failing every row.
        """
        site = normalize_site(site)
        browser_mode = normalize_browser_mode(browser_mode)
        serials = [str(serial) for serial in serials]
        with self.lock:
            if self.running:
                raise RuntimeError('已有查询在进行中')
            # 清除上一批的终止/降级状态；本次预检与正式批次共享同一套
            # start/stop 资源保护，但不清空历史结果行。
            self.stop_event = threading.Event()
            self.fatal_error = ''
            self.fatal_error_code = ''
            self._active_browser_codes = set()
            self._pending_close_codes = set()
            self._resource_constrained = False
            self._runtime_state = 'preparing'
            self._runtime_message = ''
            self._recovery_attempt = 0
            self._recovery_count = 0
            self._preflight_completed = False
            self.allow_open_environment = bool(allow_open_environment)
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
        target_browser_type = ''
        target_core_version = ''
        for serial in serials:
            env = index.get(serial)
            if not env:
                continue
            code = str(env.get('containerCode') or '').strip()
            if not code:
                continue
            if code in open_codes:
                if not self.allow_open_environment:
                    continue
                try:
                    attached = self._attach_open_browser(code)
                except HubApiError:
                    continue
                with self.lock:
                    self._preflight_completed = True
                return {
                    'checked': True,
                    'debuggingPort': int(attached['debuggingPort']),
                    'attachedExisting': True,
                }
            target_code = code
            target_browser_type = str(
                env.get('browser') or 'chrome').casefold()
            target_core_version = str(env.get('coreVersion') or '')
            break
        if not target_code:
            with self.lock:
                self._preflight_completed = True
            return {'checked': False}

        started = False
        try:
            data = self._start_browser(
                target_code, browser_mode=browser_mode)
            started = True
            try:
                debugging_port = int((data or {}).get('debuggingPort'))
            except (TypeError, ValueError):
                debugging_port = 0
            if debugging_port < 1:
                raise HubApiError(
                    'HubStudio 启动环境后未返回调试端口',
                    'hubstudio_browser_launch_invalid')
            with self.lock:
                self._preflight_completed = True
            return {'checked': True, 'debuggingPort': debugging_port}
        except HubApiError as exc:
            marker = getattr(self.hub, 'mark_runtime_failure', None)
            if (callable(marker)
                    and exc.reason_code == 'hubstudio_browser_core_missing'):
                marker(
                    exc.reason_code, str(exc),
                    browser_type=(exc.browser_type or target_browser_type),
                    core_version=(exc.core_version or target_core_version),
                    container_code=target_code)
            self._record_systemic_hub_failure(exc, fail_rows=False)
            raise
        finally:
            if started:
                if not self._stop_browser_and_confirm(target_code):
                    with self.lock:
                        self._preflight_completed = False
                    raise HubApiError(
                        '预检环境关闭确认超时，请等待 HubStudio '
                        '释放资源后重试',
                        'hubstudio_browser_cleanup_pending')

    def start_batch(self, serials, env_index=None, site='MX',
                    on_finished=None, browser_mode='headless',
                    allow_open_environment=False):
        """启动批量查询线程。env_index: {serialNumber(str): env dict}。"""
        site = normalize_site(site)
        serials = list(serials)
        self._prepare_run(
            serials, site, fresh=True, browser_mode=browser_mode,
            allow_open_environment=allow_open_environment)
        threading.Thread(
            target=self._run,
            args=(serials, env_index or {}, False, site, on_finished, True),
            daemon=True).start()

    def _prepare_run(self, serials, site, fresh, browser_mode=None,
                     allow_open_environment=None):
        """Publish running state before background I/O can block or finish."""
        with self.lock:
            if self.running:
                raise RuntimeError('已有查询在进行中')
            preserve_preflight_pressure = bool(self._preflight_completed)
            constrained = bool(
                self._resource_constrained and preserve_preflight_pressure)
            recovery_count = (
                self._recovery_count if preserve_preflight_pressure else 0)
            self._preflight_completed = False
            self.stop_event = threading.Event()
            self.site = site
            if browser_mode is not None:
                self.browser_mode = normalize_browser_mode(browser_mode)
            if allow_open_environment is not None:
                self.allow_open_environment = bool(allow_open_environment)
            self.running = True
            self.started_at = time.time()
            self.finished_at = None
            self.fatal_error = ''
            self.fatal_error_code = ''
            self._inflight = set()
            self._active_browser_codes = set()
            self._pending_close_codes = set()
            self._resource_constrained = constrained
            self._runtime_state = 'degraded' if constrained else 'running'
            self._runtime_message = (
                '预检曾检测到 HubStudio 资源压力，本批已自动降为单环境运行'
                if constrained else '')
            self._recovery_attempt = 0
            self._recovery_count = recovery_count
            if fresh:
                self._reset_screenshots()
                self.rows = [self._blank_row(s, site) for s in serials]

    def _set_runtime_recovery(self, state, message, attempt=0,
                              constrain=True):
        with self._browser_state:
            if constrain:
                self._resource_constrained = True
            self._runtime_state = str(state or 'recovering')[:64]
            self._runtime_message = str(message or '')[:180]
            self._recovery_attempt = max(0, int(attempt or 0))
            self._recovery_count += 1
            self._browser_state.notify_all()

    def _mark_browser_started(self, code):
        with self._browser_state:
            self._active_browser_codes.add(str(code))
            self._runtime_state = (
                'degraded' if self._resource_constrained else 'running')
            self._runtime_message = (
                'HubStudio 已恢复，后续查询已自动降为单环境运行'
                if self._resource_constrained else '')
            self._recovery_attempt = 0
            self._browser_state.notify_all()

    def _mark_browser_stopped(self, code):
        with self._browser_state:
            self._active_browser_codes.discard(str(code))
            self._browser_state.notify_all()

    def _wait_for_degraded_slot(self, deadline):
        """资源受限后只允许一个由本批启动的环境同时运行。"""
        with self._browser_state:
            while (self._resource_constrained
                   and self._active_browser_codes
                   and not self.stop_event.is_set()
                   and time.time() < deadline):
                self._runtime_state = 'waiting_cleanup'
                self._runtime_message = (
                    '正在等待上一环境完全关闭并释放 HubStudio 资源')
                self._browser_state.wait(timeout=min(
                    1.0, max(0.05, deadline - time.time())))

    def _wait_for_pending_closes(self, deadline):
        """资源受限时，不越过仍被 HubStudio 报告为打开的本批环境。"""
        checker = getattr(self.hub, 'open_container_codes', None)
        while time.time() < deadline and not self.stop_event.is_set():
            with self.lock:
                pending = set(self._pending_close_codes)
            if not pending:
                return True
            if not callable(checker):
                return False
            try:
                open_codes = set(checker())
            except HubApiError:
                self._set_runtime_recovery(
                    'reconnecting_hub',
                    '正在恢复 Local API，以确认上一环境已经关闭')
            else:
                remaining = pending.intersection(open_codes)
                with self._browser_state:
                    self._pending_close_codes.difference_update(
                        pending - remaining)
                    if not self._pending_close_codes:
                        self._browser_state.notify_all()
                        return True
                    self._runtime_state = 'waiting_cleanup'
                    self._runtime_message = (
                        'HubStudio 仍在关闭上一环境，暂不启动新环境')
            time.sleep(BROWSER_CLOSE_POLL_SECONDS)
        return False

    def _stop_browser_and_confirm(self, code):
        """关闭本批环境，并确认 HubStudio 不再报告为打开状态。"""
        code = str(code)
        deadline = time.time() + BROWSER_CLOSE_CONFIRM_SECONDS
        stop_sent = False
        confirmed = False
        last_error = None
        checker = getattr(self.hub, 'open_container_codes', None)
        try:
            while time.time() < deadline:
                if not stop_sent:
                    try:
                        self.hub.browser_stop(code)
                        stop_sent = True
                    except HubApiError as exc:
                        last_error = exc
                        if exc.reason_code not in {
                                'hubstudio_local_api_unreachable',
                                'hubstudio_local_api_timeout'}:
                            break
                if stop_sent and not callable(checker):
                    confirmed = True
                    break
                if stop_sent:
                    try:
                        if code not in checker():
                            confirmed = True
                            break
                    except HubApiError as exc:
                        last_error = exc
                if self.stop_event.is_set() and not self.fatal_error_code:
                    # 用户停止仍要尽量完成当前关闭，但不额外拖满 30 秒。
                    break
                time.sleep(BROWSER_CLOSE_POLL_SECONDS)
        finally:
            with self._browser_state:
                self._active_browser_codes.discard(code)
                if confirmed:
                    self._pending_close_codes.discard(code)
                else:
                    self._pending_close_codes.add(code)
                self._browser_state.notify_all()
        if not confirmed:
            detail = str(last_error or 'HubStudio 尚未确认环境完全关闭')
            self._set_runtime_recovery(
                'waiting_cleanup',
                '环境关闭确认延迟：%s；后续查询已降为单环境运行' %
                detail[:100])
        return confirmed

    def requery(self, serial, env_index=None, force=False, on_finished=None,
                site=None, allow_missing=False, browser_mode=None,
                allow_open_environment=None):
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
            if not allow_missing:
                raise ValueError('该序号不在当前结果中，请重新发起批量查询')
            site = normalize_site(site or self.site)
            self._prepare_run(
                [serial], site, fresh=True, browser_mode=browser_mode,
                allow_open_environment=allow_open_environment)
        else:
            site = row.get('site') or self.site
        if force:
            self._force_stops.add(serial)
        if row is not None:
            self._prepare_run(
                [serial], site, fresh=False, browser_mode=browser_mode,
                allow_open_environment=allow_open_environment)
        threading.Thread(
            target=self._run,
            args=([serial], env_index or {}, False, site, on_finished, True),
            daemon=True).start()

    def requery_failed(self, env_index=None, on_finished=None,
                       browser_mode=None, allow_open_environment=None):
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
        self._prepare_run(
            serials, site, fresh=False, browser_mode=browser_mode,
            allow_open_environment=allow_open_environment)
        threading.Thread(
            target=self._run,
            args=(serials, env_index or {}, False, site, on_finished, True),
            daemon=True).start()
        return len(serials)

    def request_stop(self):
        self.stop_event.set()
        with self._browser_state:
            self._browser_state.notify_all()

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
            except HubApiError as exc:
                # After the shared adapter has exhausted its recovery window,
                # an unknown open-set is not equivalent to an empty open-set.
                # Guessing here can double-start environments and compound
                # HubStudio resource pressure.
                self._set_runtime_recovery(
                    'degraded',
                    '无法确认 HubStudio 已打开环境，已安全停止本批次')
                self._fail_all(
                    '读取 HubStudio 已打开环境失败：%s' % str(exc)[:120])
                return

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

            effective_concurrency = (
                1 if (self.browser_mode == 'visible'
                      or self._resource_constrained)
                else self.concurrency)
            n_workers = max(1, min(effective_concurrency, len(serials)))
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

    def _reconcile_timed_out_start(self, code):
        """确认一次超时的 start 是否其实已由 HubStudio 执行成功。

        Local API 的 HTTP 等待超时不等于 HubStudio 取消了启动。若环境已在
        后台打开，直接再次调用 browser/start 会制造重复启动和额外资源
        压力，因此必须先通过浏览器状态接口对账。
        """
        loader = getattr(self.hub, 'browser_status', None)
        if not callable(loader):
            return None
        try:
            statuses = loader(code, timeout=5.0)
        except (HubApiError, NotImplementedError, TypeError):
            return None
        wanted = str(code)
        for item in statuses or []:
            if not isinstance(item, dict):
                continue
            if str(item.get('containerCode') or '') != wanted:
                continue
            try:
                port = int(item.get('debuggingPort') or 0)
            except (TypeError, ValueError):
                port = 0
            if port > 0:
                return dict(item)
        return None

    def _attach_open_browser(self, code):
        """只读附着一个已打开环境，不取得该环境的关闭所有权。"""
        loader = getattr(self.hub, 'browser_status', None)
        if not callable(loader):
            raise HubApiError(
                '当前 HubStudio Local API 不支持读取已打开环境状态',
                'hubstudio_browser_attach_unavailable')
        try:
            statuses = loader(code, timeout=10.0)
        except (NotImplementedError, TypeError) as exc:
            raise HubApiError(
                '当前 HubStudio Local API 不支持连接已打开环境',
                'hubstudio_browser_attach_unavailable') from exc
        wanted = str(code)
        for item in statuses or []:
            if not isinstance(item, dict):
                continue
            if str(item.get('containerCode') or '') != wanted:
                continue
            try:
                port = int(item.get('debuggingPort') or 0)
            except (TypeError, ValueError):
                port = 0
            if port > 0:
                return dict(item)
        raise HubApiError(
            '已打开环境未返回可用调试端口，请稍后重查',
            'hubstudio_browser_attach_unavailable')

    def _start_browser(self, code, browser_mode=None):
        """串行启动浏览器，并对资源压力与 Local API 波动自适应恢复。

        -10005 常见于并发场景或上一批次 start 尚在执行时同环境再次 start，
        正确处理是等待其执行完而非快速重试（默认 0.4s×3 次的重试对它无效）。

        -10008 表明 Local API 可达、但 HubStudio 无法继续分配浏览器资源。
        此时先等待本批已启动环境完全关闭，再把后续执行降为单环境。Local
        API 断连若客户端窗口仍在，也按可恢复行级故障处理，不再连带终止
        尚未开始的整批任务。
        """
        mode = normalize_browser_mode(browser_mode or self.browser_mode)
        deadline = time.time() + 180
        transport_attempts = {}
        resource_attempt = 0
        with self._start_lock:
            if self.stop_event.is_set() and self.fatal_error_code:
                raise HubApiError(
                    self.fatal_error or 'HubStudio 系统状态不可用',
                    self.fatal_error_code)
            while True:
                self._wait_for_degraded_slot(deadline)
                if (self._resource_constrained
                        and not self._wait_for_pending_closes(deadline)):
                    raise HubApiError(
                        '等待 HubStudio 完成环境关闭超时，可稍后重查此行',
                        'hubstudio_system_resources_insufficient',
                        api_code='-10008')
                with self.lock:
                    capacity_busy = bool(
                        self._resource_constrained
                        and self._active_browser_codes)
                if capacity_busy:
                    raise HubApiError(
                        '等待上一环境释放资源超时，可稍后重查此行',
                        'hubstudio_system_resources_insufficient',
                        api_code='-10008')
                try:
                    result = self.hub.browser_start(
                        code, headless=mode == 'headless')
                    self._mark_browser_started(code)
                    return result
                except HubApiError as e:
                    if '-10005' in str(e) and time.time() < deadline:
                        time.sleep(5)
                        continue
                    reason_code = str(e.reason_code or '')
                    if reason_code == 'hubstudio_system_resources_insufficient':
                        if (resource_attempt < len(HUB_RESOURCE_RECOVERY_DELAYS)
                                and time.time() < deadline):
                            resource_attempt += 1
                            self._set_runtime_recovery(
                                'recovering_resources',
                                'HubStudio 资源不足，正在等待环境释放；'
                                '后续自动降为单环境运行',
                                resource_attempt)
                            self._wait_for_degraded_slot(deadline)
                            delay = min(
                                HUB_RESOURCE_RECOVERY_DELAYS[
                                    resource_attempt - 1],
                                max(0.0, deadline - time.time()))
                            if delay > 0:
                                # 必须在 browser 控制锁外休眠，使另一个 worker
                                # 可以提交 stop；全局 defer 会反过来阻塞资源释放。
                                time.sleep(delay)
                                continue
                        self._set_runtime_recovery(
                            'degraded',
                            'HubStudio 资源在恢复窗口内仍不足；本行稍后可重查',
                            resource_attempt)
                        raise
                    recovery_delays = HUB_TRANSPORT_RECOVERY_DELAYS.get(
                        reason_code, ())
                    retry_index = transport_attempts.get(reason_code, 0)
                    if reason_code == 'hubstudio_local_api_timeout':
                        self._set_runtime_recovery(
                            'reconnecting_hub',
                            'HubStudio 启动请求响应超时，正在核对环境是否已实际打开',
                            retry_index + 1)
                        reconciled = self._reconcile_timed_out_start(code)
                        if reconciled is not None:
                            self._mark_browser_started(code)
                            return reconciled
                    client_running_getter = getattr(
                        self.hub, 'client_running_getter', None)
                    if recovery_delays and callable(client_running_getter):
                        try:
                            client_running = bool(client_running_getter())
                        except Exception:
                            # 进程诊断本身异常时不能把一次可恢复连接波动
                            # 误判成用户主动关闭 HubStudio。
                            client_running = True
                        if not client_running:
                            offline = HubApiError(
                                '未检测到 HubStudio 客户端运行',
                                'hubstudio_client_not_running')
                            self._record_systemic_hub_failure(offline)
                            raise offline
                    if (retry_index < len(recovery_delays)
                            and time.time() < deadline):
                        transport_attempts[reason_code] = retry_index + 1
                        if reason_code != 'hubstudio_local_api_timeout':
                            self._set_runtime_recovery(
                                'reconnecting_hub',
                                'HubStudio Local API 暂时无响应，正在等待本机服务恢复',
                                retry_index + 1)
                        self._wait_for_degraded_slot(deadline)
                        delay = min(
                            recovery_delays[retry_index],
                            max(0.0, deadline - time.time()),
                        )
                        if delay > 0:
                            time.sleep(delay)
                        if reason_code == 'hubstudio_local_api_timeout':
                            reconciled = self._reconcile_timed_out_start(code)
                            if reconciled is not None:
                                self._mark_browser_started(code)
                                return reconciled
                        if time.time() < deadline:
                            continue
                    if recovery_delays:
                        self._set_runtime_recovery(
                            'degraded',
                            'HubStudio Local API 在恢复窗口内仍不可达；'
                            '本行稍后可重查',
                            retry_index)
                        raise
                    self._record_systemic_hub_failure(e)
                    raise

    def _query_one(self, row, serial, env_index, open_codes, site='MX'):
        site = normalize_site(site)
        profile = _site_profile(site)
        self._remove_screenshot(serial)
        self._update(
            row, screenshotState='pending', screenshotFile='',
            screenshotError='', screenshotSizeKb=0,
            screenshotWidth=0, screenshotHeight=0,
            firstTrackingAt='', firstTrackingTime='',
            firstTrackingSummary='', firstTrackingLeadMinutes=None)
        env = env_index.get(serial)
        if env is None:
            self._update(row, state='fail', error='未找到该环境序号')
            return
        code = str(env.get('containerCode'))
        env_name = env.get('containerName') or ''
        self._update(row, envName=env_name,
                     state='running')
        data = None
        if code in open_codes:
            if serial in self._force_stops:
                # 孤儿窗口清理：上次查询中断残留的打开状态，关闭后重查
                self._force_stops.discard(serial)
                try:
                    if not self._stop_browser_and_confirm(code):
                        raise RuntimeError('HubStudio 未确认残留窗口已关闭')
                except Exception as e:
                    self._update(row, state='fail',
                                 error='关闭残留窗口失败：%s' % str(e)[:80])
                    return
            elif self.allow_open_environment:
                try:
                    data = self._attach_open_browser(code)
                except HubApiError as e:
                    self._update(
                        row, state='fail',
                        error='连接已打开环境失败：%s' % str(e)[:100])
                    return
            else:
                # 正在使用（采购同事开着或未归档）——跳过防登录态覆盖
                self._update(row, state='inuse', error=(
                    '环境浏览器处于打开状态：可在高级设置启用'
                    '「允许连接已打开环境」只读查询；'
                    '若为上次查询中断的残留窗口也可用「关闭并重查」'))
                return
        started = False
        page = None
        try:
            if data is None:
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
            # 详情页风险/退款信号优先于普通列表状态；Pagado/Paid 与风险
            # 验证同时存在时保留平台原状态，中文明确标为“已支付/待验证”。
            info, detail = merge_order_status_signals(info, detail, site)
            if detail['tracks']:
                try:
                    screenshot = self._capture_tracking(
                        page, serial, info['orderNo'], detail['tracks'], site,
                        order_time=(info.get('orderTime') or
                                    detail.get('orderTime') or ''),
                        utc_offset_minutes=utc_offset)
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
        except HubApiError as e:
            self._update(row, state='fail',
                         error='查询异常：%s' % str(e)[:120])
            self._record_systemic_hub_failure(e)
        except Exception as e:
            self._update(row, state='fail',
                         error='查询异常：%s' % str(e)[:120])
        finally:
            if page is not None:
                try:
                    page.close()
                except Exception:
                    pass
            if started:
                try:
                    self._stop_browser_and_confirm(code)
                except Exception:
                    self._mark_browser_stopped(code)
