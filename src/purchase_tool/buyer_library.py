# -*- coding: utf-8 -*-
"""Buyer-account inventory over the configured Feishu Base target.

The public surface is deliberately metadata-only.  Passwords, verification
links and cookies are accepted for an explicitly confirmed vendor import, but
never returned by list/preview APIs or written to local state files.
"""
from datetime import datetime, timedelta, timezone
import base64
import hashlib
from io import BytesIO
import threading
import time

from .env_batch import parse_vendor_workbook, validate_accounts_site
from .redaction import mask_email, scrub_text


SHANGHAI = timezone(timedelta(hours=8))
IMPORT_TTL_SECONDS = 15 * 60
IMPORT_MAX_BYTES = 20 * 1024 * 1024
PUBLIC_FIELDS = (
    '站点', '邮箱账号', '来源类型', '号商名称', '入库批次', '购买日期',
    '账号状态', '凭证状态', '绑定环境', '环境分组名', '环境序号', '采购员',
    '绑定时间', 'IP检测状态', '出口IP', '出口国家', 'ISP', 'IP检测时间',
    '异常原因', '号商购买单号',
)
IMPORT_REQUIRED_FIELDS = {
    '站点': 'SingleSelect',
    '邮箱账号': 'Text',
    '密码': 'Text',
    '接码Key链接': 'Text',
    'Cookie': 'Text',
    '号商购买单号': 'Text',
    '购买日期': 'DateTime',
    '账号状态': 'SingleSelect',
    '来源类型': 'SingleSelect',
    '号商名称': 'Text',
    '入库批次': 'Text',
    '入库时间': 'DateTime',
    '凭证状态': 'SingleSelect',
}
TEXT_ALIASES = {'Email': 'Text', 'Url': 'Text'}
STATUS_MAP = {
    '未绑定': '可用',
    '已登录': '已绑定',
    '封号': '停用',
}


class BuyerLibraryError(ValueError):
    pass


def _text(value):
    if value is None:
        return ''
    if isinstance(value, list):
        return ''.join(
            str(item.get('text') or item.get('name') or '')
            if isinstance(item, dict) else str(item)
            for item in value).strip()
    if isinstance(value, dict):
        return str(value.get('text') or value.get('name') or '').strip()
    return str(value).strip()


def _fields(record):
    value = record.get('fields') if isinstance(record, dict) else None
    return value if isinstance(value, dict) else {}


def _field_name(field):
    return str(field.get('field_name') or field.get('name') or '')


def _field_type(field):
    ui_type = str(field.get('ui_type') or '')
    if ui_type:
        return TEXT_ALIASES.get(ui_type, ui_type)
    raw = str(field.get('type') or '')
    return {
        'text': 'Text', 'select': 'SingleSelect',
        'datetime': 'DateTime', 'number': 'Number',
    }.get(raw, raw)


def _option_names(field):
    prop = field.get('property') if isinstance(field, dict) else None
    options = prop.get('options') if isinstance(prop, dict) else None
    if options is None:
        options = field.get('options') if isinstance(field, dict) else []
    return {str(item.get('name') or '') for item in (options or [])
            if isinstance(item, dict)}


def _date_ms(value):
    text = str(value or '').strip()
    for fmt in ('%Y-%m-%d', '%Y%m%d'):
        try:
            parsed = datetime.strptime(text, fmt).replace(tzinfo=SHANGHAI)
            return int(parsed.timestamp() * 1000)
        except ValueError:
            pass
    raise BuyerLibraryError('购买日期格式无效')


def _date_display(value):
    if value in (None, ''):
        return ''
    try:
        number = int(float(value))
    except (TypeError, ValueError):
        return _text(value)[:20]
    try:
        return datetime.fromtimestamp(
            number / 1000, tz=SHANGHAI).strftime('%Y-%m-%d')
    except (OverflowError, OSError, ValueError):
        return ''


def _number(value):
    if value in (None, ''):
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _schema_index(fields):
    return {_field_name(field): field for field in fields
            if isinstance(field, dict) and _field_name(field)}


def validate_library_import_schema(fields):
    index = _schema_index(fields)
    bad = [name for name, expected in IMPORT_REQUIRED_FIELDS.items()
           if _field_type(index.get(name, {})) != expected]
    if bad:
        raise BuyerLibraryError(
            '买家号库字段缺失或类型不匹配：%s' % ', '.join(bad))
    if not {'MX', 'US'}.issubset(_option_names(index['站点'])):
        raise BuyerLibraryError('买家号库「站点」必须包含 MX、US')
    if '号商采购' not in _option_names(index['来源类型']):
        raise BuyerLibraryError('买家号库「来源类型」缺少“号商采购”选项')
    if '未验证' not in _option_names(index['凭证状态']):
        raise BuyerLibraryError('买家号库「凭证状态」缺少“未验证”选项')
    status_options = _option_names(index['账号状态'])
    # Prefer the legacy status so the new table remains compatible with the
    # existing environment-ledger write-back contract.
    if '未绑定' in status_options:
        available_status = '未绑定'
    elif '可用' in status_options:
        available_status = '可用'
    else:
        raise BuyerLibraryError(
            '买家号库「账号状态」缺少“可用”或兼容选项“未绑定”')
    return index, available_status


class BuyerLibraryService(object):
    def __init__(self, client):
        self.client = client

    def list_public(self, site='', status='', limit=100):
        site = str(site or '').strip().upper()
        if site and site not in {'MX', 'US'}:
            raise BuyerLibraryError('站点筛选仅支持 MX 或 US')
        try:
            limit = max(1, min(500, int(limit)))
        except (TypeError, ValueError) as exc:
            raise BuyerLibraryError('列表数量参数无效') from exc
        fields = self.client.list_fields()
        index = _schema_index(fields)
        selected = [name for name in PUBLIC_FIELDS if name in index]
        if '邮箱账号' not in selected or '账号状态' not in selected:
            raise BuyerLibraryError('当前飞书目标不是可识别的买家号表')
        records = self.client.list_records(selected)
        rows = []
        matching_total = 0
        counts = {'total': 0, 'available': 0, 'reserved': 0,
                  'bound': 0, 'abnormal': 0, 'disabled': 0}
        for record in records:
            values = _fields(record)
            row_site = _text(values.get('站点')).upper()
            raw_status = _text(values.get('账号状态'))
            public_status = STATUS_MAP.get(raw_status, raw_status or '待校验')
            counts['total'] += 1
            if public_status == '可用':
                counts['available'] += 1
            elif public_status in {'已预占', '绑定中'}:
                counts['reserved'] += 1
            elif public_status == '已绑定':
                counts['bound'] += 1
            elif public_status == '异常':
                counts['abnormal'] += 1
            elif public_status == '停用':
                counts['disabled'] += 1
            if site and row_site != site:
                continue
            if status and public_status != status:
                continue
            matching_total += 1
            if len(rows) >= limit:
                continue
            email = _text(values.get('邮箱账号'))
            rows.append({
                'accountId': hashlib.sha256(
                    email.casefold().encode('utf-8')).hexdigest(),
                'emailMasked': mask_email(email),
                'site': row_site,
                'source': _text(values.get('来源类型')) or '未标记',
                'vendor': _text(values.get('号商名称')),
                'batchNo': _text(values.get('入库批次')),
                'purchaseDate': _date_display(values.get('购买日期')),
                'status': public_status,
                'credentialStatus': _text(values.get('凭证状态')) or '未标记',
                'buyer': _text(values.get('采购员')),
                'envName': _text(values.get('绑定环境')),
                'serialNumber': _number(values.get('环境序号')),
                'ipStatus': _text(values.get('IP检测状态')) or '未检测',
            })
        return {'connected': True, 'counts': counts, 'rows': rows,
                'visible': len(rows), 'truncated': matching_total > len(rows)}

    def import_preflight(self, accounts, site):
        validate_accounts_site(accounts, site)
        fields = self.client.list_fields()
        _index, available_status = validate_library_import_schema(fields)
        records = self.client.list_records(
            ['站点', '邮箱账号', '号商购买单号', '账号状态'])
        by_email = {}
        by_order = {}
        for record in records:
            values = _fields(record)
            email = _text(values.get('邮箱账号')).casefold()
            order = _text(values.get('号商购买单号')).casefold()
            if email:
                by_email[email] = by_email.get(email, 0) + 1
            if order:
                by_order[order] = by_order.get(order, 0) + 1
        conflicts = []
        for account in accounts:
            messages = []
            if by_email.get(account.email.strip().casefold()):
                messages.append('邮箱已存在')
            if by_order.get(account.order_no.strip().casefold()):
                messages.append('号商单号已存在')
            if messages:
                conflicts.append({
                    'accountId': account.account_id,
                    'emailMasked': account.safe_email,
                    'message': '、'.join(messages),
                })
        return {'ready': not conflicts, 'total': len(accounts),
                'conflicts': len(conflicts), 'rows': conflicts,
                'availableStatus': available_status}

    def import_accounts(self, accounts, site, vendor_name, batch_no,
                        purchase_date, confirm_write=False):
        if not confirm_write:
            raise BuyerLibraryError('正式入库必须二次确认飞书写入')
        preflight = self.import_preflight(accounts, site)
        if preflight['conflicts']:
            raise BuyerLibraryError(
                '飞书买家号库发现 %d 条重复记录，已阻止整批入库' %
                preflight['conflicts'])
        fields = self.client.list_fields()
        _index, available_status = validate_library_import_schema(fields)
        purchase_ms = _date_ms(purchase_date)
        now_ms = int(datetime.now(SHANGHAI).timestamp() * 1000)
        payloads = []
        for account in accounts:
            payload = {
                '站点': site,
                '邮箱账号': account.email,
                '密码': account.password,
                '接码Key链接': {
                    'text': account.key_url,
                    'link': account.key_url,
                },
                'Cookie': account.cookie_text,
                '号商购买单号': account.order_no,
                '购买日期': purchase_ms,
                '账号状态': available_status,
            }
            source_metadata = {
                '来源类型': '号商采购',
                '号商名称': vendor_name,
                '入库批次': batch_no,
                '入库时间': now_ms,
                '凭证状态': '未验证',
            }
            payload.update(source_metadata)
            payloads.append(payload)
        created = []
        try:
            for offset in range(0, len(payloads), 100):
                created.extend(self.client.batch_create(
                    payloads[offset:offset + 100]))
        except Exception as exc:
            text = str(exc)
            for account in accounts:
                for secret in (account.email, account.password,
                               account.key_url, account.cookie_text):
                    if secret:
                        text = text.replace(secret, '<redacted>')
            prefix = ('买家号入库已写入 %d 条后中断，请先核对飞书，'
                      '不要直接重试：' % len(created)) if created else '买家号入库失败：'
            raise BuyerLibraryError(
                prefix + scrub_text(text)[:200]) from exc
        if len(created) != len(accounts):
            raise BuyerLibraryError('飞书新增返回数量不一致，请立即核对目标表')
        return {'created': len(created), 'site': site,
                'batchNo': batch_no}


class BuyerLibraryJob(object):
    """In-memory vendor-import plans with an explicit remote-write commit."""
    def __init__(self, service_factory):
        self.service_factory = service_factory
        self.lock = threading.Lock()
        # Serialize remote writes so two different short-lived plans cannot
        # both pass the duplicate check before either one reaches Feishu.
        self.commit_lock = threading.Lock()
        self.pending = {}

    def _clean_pending(self):
        now = time.time()
        expired = [key for key, value in self.pending.items()
                   if now - value['createdAt'] > IMPORT_TTL_SECONDS]
        for key in expired:
            self.pending.pop(key, None)

    def list_public(self, site='', status='', limit=100):
        return self.service_factory().list_public(site, status, limit)

    def parse(self, filename, content_base64, site, vendor_name, batch_no,
              purchase_date):
        name = str(filename or '').strip()
        if not name.lower().endswith('.xlsx'):
            raise BuyerLibraryError('号商入库仅支持 xlsx 文件')
        vendor_name = str(vendor_name or '').strip()
        batch_no = str(batch_no or '').strip()
        if not vendor_name or len(vendor_name) > 50:
            raise BuyerLibraryError('号商名称不能为空且不能超过50个字符')
        if not batch_no or len(batch_no) > 80:
            raise BuyerLibraryError('入库批次不能为空且不能超过80个字符')
        site = str(site or '').strip().upper()
        if site not in {'MX', 'US'}:
            raise BuyerLibraryError('入库站点仅支持 MX 或 US')
        _date_ms(purchase_date)
        try:
            source = base64.b64decode(
                str(content_base64 or ''), validate=True)
        except Exception as exc:
            raise BuyerLibraryError('xlsx 内容编码无效') from exc
        if not source or len(source) > IMPORT_MAX_BYTES:
            raise BuyerLibraryError('xlsx 文件为空或超过20MB')
        accounts = parse_vendor_workbook(BytesIO(source))
        validate_accounts_site(accounts, site)
        library_ready = True
        library_error = ''
        conflicts = []
        try:
            result = self.service_factory().import_preflight(accounts, site)
            conflicts = result['rows']
        except Exception as exc:
            library_ready = False
            library_error = scrub_text(exc)[:200]
        plan_id = hashlib.sha256(
            source + ('\0%s\0%s\0%s\0%s' % (
                site, vendor_name, batch_no, purchase_date)).encode('utf-8')
        ).hexdigest()
        with self.lock:
            self._clean_pending()
            self.pending[plan_id] = {
                'accounts': accounts,
                'site': site,
                'vendorName': vendor_name,
                'batchNo': batch_no,
                'purchaseDate': purchase_date,
                'createdAt': time.time(),
            }
        return {
            'planId': plan_id,
            'count': len(accounts),
            'cookieCount': sum(bool(item.cookie_text) for item in accounts),
            'libraryReady': library_ready,
            'libraryError': library_error,
            'conflicts': len(conflicts),
            'conflictRows': conflicts[:20],
            'preview': [{
                'accountId': item.account_id,
                'emailMasked': item.safe_email,
                'cookieReady': bool(item.cookie_text),
            } for item in accounts[:20]],
        }

    def commit(self, plan_id, confirm_write=False):
        with self.commit_lock:
            with self.lock:
                self._clean_pending()
                pending = self.pending.get(str(plan_id or ''))
            if not pending:
                raise BuyerLibraryError('入库计划已过期，请重新选择xlsx')
            result = self.service_factory().import_accounts(
                pending['accounts'], pending['site'], pending['vendorName'],
                pending['batchNo'], pending['purchaseDate'],
                confirm_write=confirm_write)
            with self.lock:
                self.pending.pop(str(plan_id or ''), None)
            return result
