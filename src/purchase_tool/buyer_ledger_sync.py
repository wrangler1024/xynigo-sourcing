# -*- coding: utf-8 -*-
"""Idempotent write-back of successful HubStudio rows to one buyer Base.

The unified table is keyed by normalized email plus vendor order number.  The
service never overwrites existing credentials and never exposes record IDs or
raw account values in its public result.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .env_batch import normalize_env_site
from .lark_openapi import LarkApiError
from .redaction import mask_email, scrub_text


SHANGHAI = timezone(timedelta(hours=8))
IDENTITY_FIELDS = (
    '站点', '邮箱账号', '号商购买单号', '购买日期', '账号状态',
    '绑定环境', '环境分组名', '环境序号', '采购员', '绑定时间',
)
CREATE_FIELD_TYPES = {
    '站点': 'SingleSelect',
    '邮箱账号': 'Text',
    '密码': 'Text',
    '接码Key链接': 'Text',
    'Cookie': 'Text',
    '号商购买单号': 'Text',
    '购买日期': 'DateTime',
    '账号状态': 'SingleSelect',
    '绑定环境': 'Text',
    '环境分组名': 'Text',
    '环境序号': 'Number',
    '采购员': 'SingleSelect',
    '绑定时间': 'DateTime',
    '首次登录日期': 'DateTime',
}
NORMALIZED_TYPES = {
    'text': 'Text', 'select': 'SingleSelect', 'datetime': 'DateTime',
    'number': 'Number', 'auto_number': 'AutoNumber',
}
TEXT_UI_TYPE_ALIASES = {
    'Email': 'Text',
    'Url': 'Text',
}
SAFE_BOUND_STATUSES = {'已绑定', '已登录'}
BLOCKING_STATUSES = {'异常', '封号', '停用'}
SITE_OPTIONS = {'MX', 'US'}
ACCOUNT_STATUS_OPTIONS = {
    '未绑定', '已绑定', '已登录', '异常', '封号', '停用',
}


class BuyerLedgerSyncError(Exception):
    pass


def _date_ms(value, date_only=False):
    text = str(value or '').strip()
    fmt = '%Y%m%d' if date_only else '%Y-%m-%d %H:%M:%S'
    try:
        parsed = datetime.strptime(text, fmt).replace(tzinfo=SHANGHAI)
    except ValueError as exc:
        label = '购买日期' if date_only else '绑定时间'
        raise BuyerLedgerSyncError('%s格式无效' % label) from exc
    return int(parsed.timestamp() * 1000)


def _text(value):
    if value is None:
        return ''
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        parts = []
        for item in value:
            if isinstance(item, dict):
                parts.append(str(item.get('text') or item.get('name') or ''))
            else:
                parts.append(str(item))
        return ''.join(parts).strip()
    if isinstance(value, dict):
        return str(value.get('text') or value.get('name') or '').strip()
    return str(value).strip()


def _number(value):
    if value in (None, ''):
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _datetime_value(value):
    if value in (None, ''):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    text = _text(value)
    if text.isdigit():
        return int(text)
    for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%d %H:%M', '%Y-%m-%d'):
        try:
            return int(datetime.strptime(text, fmt).replace(
                tzinfo=SHANGHAI).timestamp() * 1000)
        except ValueError:
            pass
    return None


def _fields(record):
    fields = record.get('fields') if isinstance(record, dict) else None
    return fields if isinstance(fields, dict) else {}


def _record_id(record):
    return str((record or {}).get('record_id') or (record or {}).get('id') or '')


def _field_type(field):
    ui_type = str(field.get('ui_type') or '')
    if ui_type:
        # Feishu exposes text fields with email/link display styles as
        # distinct UI types even though their OpenAPI value contract remains
        # a plain string.  Treat only those documented subtypes as Text; keep
        # every other type strict so a real schema mismatch is still blocked.
        return TEXT_UI_TYPE_ALIASES.get(ui_type, ui_type)
    raw = field.get('type')
    if isinstance(raw, str):
        return NORMALIZED_TYPES.get(raw, raw)
    return str(raw or '')


def _option_names(field):
    property_value = field.get('property')
    if isinstance(property_value, dict):
        options = property_value.get('options') or []
    else:
        options = field.get('options') or []
    return {str(item.get('name') or '') for item in options
            if isinstance(item, dict)}


def validate_unified_schema(fields, buyers=()):
    index = {
        str(field.get('field_name') or field.get('name') or ''): field
        for field in fields if isinstance(field, dict)
    }
    bad = [
        name for name, expected in CREATE_FIELD_TYPES.items()
        if _field_type(index.get(name, {})) != expected
    ]
    if bad:
        raise BuyerLedgerSyncError(
            '统一买家号台账字段缺失或类型不匹配：%s' % ', '.join(bad))
    if _option_names(index['站点']) != SITE_OPTIONS:
        raise BuyerLedgerSyncError(
            '统一买家号台账「站点」选项必须严格为 MX/US')
    if _option_names(index['账号状态']) != ACCOUNT_STATUS_OPTIONS:
        raise BuyerLedgerSyncError(
            '统一买家号台账「账号状态」选项与系统契约不一致')
    missing_buyers = sorted(
        {str(value or '') for value in buyers if value}
        - _option_names(index['采购员']))
    if missing_buyers:
        raise BuyerLedgerSyncError(
            '统一买家号台账缺少采购员选项：%s' % ', '.join(missing_buyers))
    return index


@dataclass(frozen=True)
class BuyerLedgerTarget:
    account_id: str
    row_number: int
    site: str
    email: str
    password: str
    key_url: str
    cookie_text: str
    order_no: str
    purchase_date_ms: int
    env_name: str
    environment_group: str
    serial_number: int
    buyer: str
    binding_time_ms: int

    @property
    def email_key(self):
        return self.email.strip().casefold()

    @property
    def order_key(self):
        return self.order_no.strip().casefold()

    @classmethod
    def from_plan_row(cls, row, site, purchase_date, environment_group):
        if getattr(row, 'state', '') != 'done':
            raise BuyerLedgerSyncError('只有环境成功行可以回写飞书')
        account = row.account
        try:
            serial = int(row.serial_number)
        except (TypeError, ValueError) as exc:
            raise BuyerLedgerSyncError('环境成功行缺少有效环境序号') from exc
        binding_time = str(row.binding_time or '').strip()
        if not binding_time:
            binding_time = datetime.now(SHANGHAI).strftime('%Y-%m-%d %H:%M:%S')
        environment_group = str(environment_group or '').strip()
        if not environment_group:
            raise BuyerLedgerSyncError('环境分组名不能为空')
        return cls(
            account_id=account.account_id,
            row_number=int(account.row_number),
            site=normalize_env_site(site),
            email=str(account.email or '').strip(),
            password=str(account.password or ''),
            key_url=str(account.key_url or ''),
            cookie_text=str(account.cookie_text or ''),
            order_no=str(account.order_no or '').strip(),
            purchase_date_ms=_date_ms(purchase_date, date_only=True),
            env_name=str(row.env_name or '').strip(),
            environment_group=environment_group,
            serial_number=serial,
            buyer=str(account.buyer or '').strip(),
            binding_time_ms=_date_ms(binding_time),
        )

    def create_fields(self):
        return {
            '站点': self.site,
            '邮箱账号': self.email,
            '密码': self.password,
            # Feishu raw OpenAPI type 15 (Url) requires an object.  The
            # lark-cli shortcut accepts a string and normalizes it internally,
            # but this service calls the OpenAPI directly.
            '接码Key链接': {
                'text': self.key_url,
                'link': self.key_url,
            },
            'Cookie': self.cookie_text,
            '号商购买单号': self.order_no,
            '购买日期': self.purchase_date_ms,
            '账号状态': '已绑定',
            '绑定环境': self.env_name,
            '环境分组名': self.environment_group,
            '环境序号': self.serial_number,
            '采购员': self.buyer,
            '绑定时间': self.binding_time_ms,
        }


@dataclass
class _LedgerRowResult:
    target: BuyerLedgerTarget
    state: str
    message: str = ''
    record_id: str = ''

    def public_dict(self):
        return {
            'accountId': self.target.account_id,
            'rowNumber': self.target.row_number,
            'emailMasked': mask_email(self.target.email),
            'site': self.target.site,
            'state': self.state,
            'message': scrub_text(self.message)[:200],
        }


class _RecordIndexes(object):
    def __init__(self, records):
        self.records = list(records or [])
        self.by_email = {}
        self.by_order = {}
        for record in self.records:
            fields = _fields(record)
            email = _text(fields.get('邮箱账号')).casefold()
            order = _text(fields.get('号商购买单号')).casefold()
            if email:
                self.by_email.setdefault(email, []).append(record)
            if order:
                self.by_order.setdefault(order, []).append(record)

    def find(self, target):
        emails = self.by_email.get(target.email_key, [])
        orders = self.by_order.get(target.order_key, [])
        if len(emails) > 1 or len(orders) > 1:
            return 'conflict', None, '台账业务键命中多条记录'
        if not emails and not orders:
            return 'create', None, ''
        if not emails or not orders:
            return 'conflict', None, '邮箱与号商单号未同时命中同一记录'
        if _record_id(emails[0]) != _record_id(orders[0]):
            return 'conflict', None, '邮箱与号商单号指向不同记录'
        record = emails[0]
        fields = _fields(record)
        if _text(fields.get('站点')).upper() != target.site:
            return 'conflict', record, '账号已存在于另一站点'
        env_name = _text(fields.get('绑定环境'))
        environment_group = _text(fields.get('环境分组名'))
        serial = _number(fields.get('环境序号'))
        buyer = _text(fields.get('采购员'))
        purchase_date = _datetime_value(fields.get('购买日期'))
        status = _text(fields.get('账号状态'))
        if status in BLOCKING_STATUSES:
            return 'conflict', record, '账号状态不允许自动回写'
        if env_name and env_name != target.env_name:
            return 'conflict', record, '账号已绑定到其他环境'
        if serial is not None and serial != target.serial_number:
            return 'conflict', record, '账号已有不同环境序号'
        if buyer and buyer != target.buyer:
            return 'conflict', record, '账号已有不同采购员'
        if (purchase_date is not None
                and purchase_date != target.purchase_date_ms):
            return 'conflict', record, '账号已有不同购买日期'
        if (env_name == target.env_name
                and serial == target.serial_number
                and buyer == target.buyer
                and status in SAFE_BOUND_STATUSES):
            if environment_group != target.environment_group:
                return 'update', record, ''
            return 'confirmed', record, ''
        if status == '未绑定':
            return 'update', record, ''
        if status in SAFE_BOUND_STATUSES:
            return 'conflict', record, '已绑定记录的环境字段不完整'
        return 'conflict', record, '账号状态不允许自动补全'


def _matches_target(record, target, require_credentials=False):
    fields = _fields(record)
    if (_text(fields.get('站点')).upper() != target.site
            or _text(fields.get('邮箱账号')).casefold() != target.email_key
            or _text(fields.get('号商购买单号')).casefold() != target.order_key
            or _text(fields.get('绑定环境')) != target.env_name
            or _text(fields.get('环境分组名')) != target.environment_group
            or _number(fields.get('环境序号')) != target.serial_number
            or _text(fields.get('采购员')) != target.buyer
            or _text(fields.get('账号状态')) not in SAFE_BOUND_STATUSES):
        return False
    if _datetime_value(fields.get('购买日期')) != target.purchase_date_ms:
        return False
    binding_time = _datetime_value(fields.get('绑定时间'))
    if binding_time is None:
        return False
    # A newly-created row must round-trip the exact timestamp that was sent.
    # Existing rows may keep an earlier legitimate binding timestamp because
    # update mode deliberately does not overwrite populated business fields.
    if require_credentials and binding_time != target.binding_time_ms:
        return False
    if require_credentials:
        return (
            _text(fields.get('密码')) == target.password
            and _text(fields.get('接码Key链接')) == target.key_url
            and _text(fields.get('Cookie')) == target.cookie_text
        )
    return True


def _safe_external_error(exc, targets):
    text = str(exc)
    for target in targets:
        for value in (target.email, target.password, target.key_url,
                      target.cookie_text, target.order_no):
            if value:
                text = text.replace(value, '<redacted>')
    return scrub_text(text)[:200]


class BuyerLedgerSyncService(object):
    def __init__(self, client):
        self.client = client

    def _targets(self, rows, site, purchase_date, environment_group):
        targets = [BuyerLedgerTarget.from_plan_row(
            row, site, purchase_date, environment_group) for row in rows
            if getattr(row, 'state', '') == 'done']
        emails, orders = {}, {}
        for target in targets:
            emails.setdefault(target.email_key, []).append(target)
            orders.setdefault(target.order_key, []).append(target)
        duplicate_ids = {
            item.account_id
            for values in list(emails.values()) + list(orders.values())
            if len(values) > 1 for item in values
        }
        return targets, duplicate_ids

    def _schema_and_records(self, targets):
        fields = self.client.list_fields()
        validate_unified_schema(fields, [target.buyer for target in targets])
        return self.client.list_records(IDENTITY_FIELDS)

    def preflight_plan(self, rows, site, environment_group):
        """Read-only guard before any HubStudio write is started."""
        site = normalize_env_site(site)
        if not str(environment_group or '').strip():
            raise BuyerLedgerSyncError('环境分组名不能为空')
        planned = [row for row in rows if getattr(row, 'account', None)]
        fields = self.client.list_fields()
        validate_unified_schema(
            fields, [str(row.account.buyer or '') for row in planned])
        indexes = _RecordIndexes(self.client.list_records(IDENTITY_FIELDS))
        email_counts, order_counts = {}, {}
        for row in planned:
            email_key = str(row.account.email or '').strip().casefold()
            order_key = str(row.account.order_no or '').strip().casefold()
            email_counts[email_key] = email_counts.get(email_key, 0) + 1
            order_counts[order_key] = order_counts.get(order_key, 0) + 1
        conflicts = []
        for row in planned:
            account = row.account
            email_key = str(account.email or '').strip().casefold()
            order_key = str(account.order_no or '').strip().casefold()
            message = ''
            record = None
            emails = indexes.by_email.get(email_key, [])
            orders = indexes.by_order.get(order_key, [])
            if email_counts[email_key] > 1 or order_counts[order_key] > 1:
                message = '本批次存在重复业务键'
            elif len(emails) > 1 or len(orders) > 1:
                message = '台账业务键命中多条记录'
            elif not emails and not orders:
                pass
            elif not emails or not orders:
                message = '邮箱与号商单号未同时命中同一记录'
            elif _record_id(emails[0]) != _record_id(orders[0]):
                message = '邮箱与号商单号指向不同记录'
            else:
                record = emails[0]
                record_fields = _fields(record)
                if _text(record_fields.get('站点')).upper() != site:
                    message = '账号已存在于另一站点'
                elif _text(record_fields.get('账号状态')) in BLOCKING_STATUSES:
                    message = '账号状态不允许自动回写'
                elif (_text(record_fields.get('绑定环境'))
                      and _text(record_fields.get('绑定环境')) != row.env_name):
                    message = '账号已绑定到其他环境'
                elif (_text(record_fields.get('采购员'))
                      and _text(record_fields.get('采购员'))
                      != str(account.buyer or '').strip()):
                    message = '账号已有不同采购员'
                elif (_text(record_fields.get('账号状态'))
                      in SAFE_BOUND_STATUSES
                      and not _text(record_fields.get('绑定环境'))):
                    message = '已绑定记录缺少绑定环境'
                elif (_text(record_fields.get('账号状态'))
                      not in SAFE_BOUND_STATUSES | {'未绑定'}):
                    message = '账号状态不允许自动补全'
            if message:
                conflicts.append({
                    'accountId': account.account_id,
                    'rowNumber': int(account.row_number),
                    'emailMasked': mask_email(account.email),
                    'site': site,
                    'state': 'conflict',
                    'message': message,
                })
        return {'total': len(planned), 'conflicts': len(conflicts),
                'rows': conflicts}

    def preflight(self, rows, site, purchase_date, environment_group):
        targets, duplicate_ids = self._targets(
            rows, site, purchase_date, environment_group)
        records = self._schema_and_records(targets)
        indexes = _RecordIndexes(records)
        conflicts = []
        for target in targets:
            if target.account_id in duplicate_ids:
                conflicts.append(_LedgerRowResult(
                    target, 'conflict', '本批次存在重复业务键'))
                continue
            action, _record, message = indexes.find(target)
            if action == 'conflict':
                conflicts.append(_LedgerRowResult(
                    target, 'conflict', message))
        return {
            'total': len(targets),
            'conflicts': len(conflicts),
            'rows': [item.public_dict() for item in conflicts],
        }

    @staticmethod
    def _update_fields(record, target):
        fields = _fields(record)
        patch = {
            '绑定环境': target.env_name,
            '环境分组名': target.environment_group,
            '环境序号': target.serial_number,
            '采购员': target.buyer,
        }
        if _text(fields.get('账号状态')) not in SAFE_BOUND_STATUSES:
            patch['账号状态'] = '已绑定'
        if _datetime_value(fields.get('购买日期')) is None:
            patch['购买日期'] = target.purchase_date_ms
        if _datetime_value(fields.get('绑定时间')) is None:
            patch['绑定时间'] = target.binding_time_ms
        return patch

    def _recover_after_error(self, target, error):
        try:
            records = self.client.list_records(IDENTITY_FIELDS)
            action, record, _message = _RecordIndexes(records).find(target)
            if action == 'confirmed' and _matches_target(record, target):
                return _LedgerRowResult(
                    target, 'confirmed', '请求异常但写后回读已确认',
                    _record_id(record))
        except Exception:
            pass
        # Once a write was attempted, any non-confirmed state is uncertain.
        # Do not relabel it as a deterministic pre-write conflict: a timeout or
        # partial remote write must remain retryable and visible as pending.
        return _LedgerRowResult(
            target, 'pending', _safe_external_error(error, [target]))

    def sync(self, rows, site, purchase_date, environment_group):
        targets, duplicate_ids = self._targets(
            rows, site, purchase_date, environment_group)
        records = self._schema_and_records(targets)
        indexes = _RecordIndexes(records)
        results = []
        for target in targets:
            if target.account_id in duplicate_ids:
                results.append(_LedgerRowResult(
                    target, 'conflict', '本批次存在重复业务键'))
                continue
            action, record, message = indexes.find(target)
            if action == 'conflict':
                results.append(_LedgerRowResult(target, 'conflict', message))
                continue
            if action == 'confirmed':
                results.append(_LedgerRowResult(
                    target, 'confirmed', record_id=_record_id(record)))
                continue
            try:
                if action == 'create':
                    response = self.client.batch_create([target.create_fields()])
                    written = response[0] if response else {}
                    record_id = _record_id(written)
                    if not record_id:
                        raise LarkApiError('飞书新增未返回记录标识')
                    readback = self.client.get_record(record_id)
                    if not _matches_target(
                            readback, target, require_credentials=True):
                        raise LarkApiError('飞书新增写后回读字段不一致')
                    results.append(_LedgerRowResult(
                        target, 'created', record_id=record_id))
                else:
                    record_id = _record_id(record)
                    self.client.batch_update([(
                        record_id, self._update_fields(record, target))])
                    readback = self.client.get_record(record_id)
                    if not _matches_target(readback, target):
                        raise LarkApiError('飞书更新写后回读字段不一致')
                    results.append(_LedgerRowResult(
                        target, 'updated', record_id=record_id))
            except Exception as exc:
                results.append(self._recover_after_error(target, exc))

            # Rebuild the in-memory index after each definitive write so a
            # later row in the same batch cannot bypass the dual-key guard.
            if results[-1].state in {'created', 'updated', 'confirmed'}:
                records = self.client.list_records(IDENTITY_FIELDS)
                indexes = _RecordIndexes(records)

        # Final global readback proves uniqueness and the non-sensitive
        # identity/binding contract for every row reported as successful.
        try:
            final_indexes = _RecordIndexes(
                self.client.list_records(IDENTITY_FIELDS))
            for item in results:
                if item.state not in {'created', 'updated', 'confirmed'}:
                    continue
                action, record, message = final_indexes.find(item.target)
                if action != 'confirmed' or not _matches_target(record, item.target):
                    item.state = 'pending'
                    item.message = message or '飞书写后最终回读未确认'
                    item.record_id = ''
        except Exception as exc:
            safe_error = _safe_external_error(exc, targets)
            for item in results:
                if item.state in {'created', 'updated', 'confirmed'}:
                    item.state = 'pending'
                    item.message = safe_error
                    item.record_id = ''

        counts = {name: sum(item.state == name for item in results)
                  for name in ('created', 'updated', 'confirmed',
                               'conflict', 'pending')}
        return {
            'total': len(results),
            **counts,
            'rows': [item.public_dict() for item in results],
        }
