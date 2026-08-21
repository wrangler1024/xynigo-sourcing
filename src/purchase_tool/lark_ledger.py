# -*- coding: utf-8 -*-
"""Registration-module write-back through the shared Feishu OpenAPI client.

Only a known record_id may be updated here.  Creating/deduplicating buyer rows
belongs to :mod:`buyer_ledger_sync` so the registration path cannot guess or
silently create a duplicate record.
"""
from datetime import datetime, timedelta, timezone
import os

from .redaction import scrub_text


# Kept for the legacy, administrator-only ``ledger_backfill`` command.  The
# Web application no longer shells out to lark-cli or relies on these values.
BASE_TOKEN = os.environ.get('XYNIGO_LARK_BASE_TOKEN', '')
TABLE_MX = (os.environ.get('XYNIGO_LARK_TABLE_ID_MX') or
            os.environ.get('XYNIGO_LARK_TABLE_ID', ''))
TABLE_US = os.environ.get('XYNIGO_LARK_TABLE_ID_US', '')

REQUIRED_FIELDS = {
    '站点': {'SingleSelect', 'select'},
    '账号状态': {'SingleSelect', 'select'},
    '绑定环境': {'Text', 'text'},
    '环境序号': {'Number', 'number'},
    '绑定时间': {'DateTime', 'datetime'},
    '首次登录日期': {'DateTime', 'datetime'},
    'Cookie': {'Text', 'text'},
}
SHANGHAI = timezone(timedelta(hours=8))


class LarkLedgerError(Exception):
    pass


def _field_type(field):
    return str(field.get('ui_type') or field.get('type') or '')


def _field_name(field):
    return str(field.get('field_name') or field.get('name') or '')


def _record_fields(record):
    fields = (record or {}).get('fields')
    return fields if isinstance(fields, dict) else {}


def _text(value):
    if isinstance(value, list):
        return ''.join(str(item.get('text') or item.get('name') or '')
                       if isinstance(item, dict) else str(item)
                       for item in value)
    return str(value or '')


class LarkLedgerSink(object):
    def __init__(self, client=None):
        self.client = client
        self._schema_validated = False

    @staticmethod
    def _cookie_text(cookie):
        import json
        value = cookie
        if isinstance(cookie, dict) and 'cookie' in cookie:
            value = cookie['cookie']
        if isinstance(value, str):
            return value
        return json.dumps(value, ensure_ascii=False, separators=(',', ':'))

    def build_payload(self, task, env, cookie):
        serial = env.get('serialNumber')
        if serial is None:
            raise LarkLedgerError('环境缺少 serialNumber，无法回写台账')
        now_ms = int(datetime.now(SHANGHAI).timestamp() * 1000)
        payload = {
            '站点': str(getattr(task, 'site', 'MX') or 'MX').upper(),
            '账号状态': '已绑定',
            '绑定环境': env.get('containerName') or task.env_name,
            '环境序号': int(serial),
            '绑定时间': now_ms,
            '首次登录日期': now_ms,
            'Cookie': self._cookie_text(cookie),
        }
        if getattr(task, 'buyer', ''):
            payload['采购员'] = task.buyer
        return payload

    def _validate_schema(self):
        if self._schema_validated:
            return
        if self.client is None:
            raise LarkLedgerError('飞书 OpenAPI 客户端未配置')
        fields = self.client.list_fields()
        actual = {_field_name(field): _field_type(field) for field in fields}
        bad = [name for name, accepted in REQUIRED_FIELDS.items()
               if actual.get(name) not in accepted]
        if bad:
            raise LarkLedgerError('飞书台账字段不匹配：%s' % ', '.join(bad))
        self._schema_validated = True

    def preflight(self):
        """Read-only schema validation before any surrounding platform write."""
        self._validate_schema()
        return {'ready': True}

    def __call__(self, task, env, cookie):
        if not task.record_id:
            raise LarkLedgerError('缺少 record_id，不自动创建或猜测台账记录')
        self._validate_schema()
        payload = self.build_payload(task, env, cookie)
        try:
            self.client.batch_update([(task.record_id, payload)])
            record = self.client.get_record(task.record_id)
        except Exception as exc:
            message = scrub_text(exc)
            for value in (getattr(task, 'email', ''),
                          getattr(task, 'shein_password', ''),
                          payload.get('Cookie'),
                          getattr(task, 'record_id', '')):
                if value:
                    message = message.replace(str(value), '<redacted>')
            raise LarkLedgerError('飞书回写失败：%s' % message[:200]) from exc
        fields = _record_fields(record)
        try:
            serial = int(fields.get('环境序号'))
        except (TypeError, ValueError):
            serial = None
        if (_text(fields.get('站点')).upper() != payload['站点']
                or _text(fields.get('账号状态')) not in {'已绑定', '已登录'}
                or _text(fields.get('绑定环境')) != payload['绑定环境']
                or serial != payload['环境序号']
                or _text(fields.get('Cookie')) != payload['Cookie']):
            raise LarkLedgerError('飞书回写后回读字段不一致')
        return {'updated': True}
