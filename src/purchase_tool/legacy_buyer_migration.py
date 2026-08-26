# -*- coding: utf-8 -*-
"""One-time full buyer migration from the legacy unified buyer Base.

The module is not imported by the web runtime. Source rows and credentials stay
in process memory until the cloud API encrypts them; terminal output contains
counts only. The new Base remains outbound-only after migration.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

from .cloud_auth import LocalAuthService
from .lark_credentials import system_credential_store
from .lark_runtime import build_buyer_ledger_service
from .redaction import mask_email


LEGACY_TABLE_NAME = '买家号（统一）'
SOURCE_FIELDS = (
    'Cookie', '绑定时间', '首次登录日期', '绑定环境', '账号状态', '采购员',
    '异常记录', '备注', '号商购买单号', '操作人', '邮箱账号', '最后使用日期',
    '创建时间', '环境序号', '接码Key链接', '环境分组名', '账号ID', '创建人',
    '累计下单数', '站点', '购买日期', '密码', '迁移状态',
)
AVAILABLE_STATUSES = frozenset({'未绑定', '可用'})
DISABLED_STATUSES = frozenset({'封号', '停用'})
CREDENTIAL_STATUS = {
    '验证通过': 'ready',
    '未验证': 'unverified',
    '验证失败': 'invalid',
}


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


def _purchase_date(value):
    if value in (None, ''):
        return None
    try:
        timestamp = int(float(value)) / 1000
        return datetime.fromtimestamp(timestamp, tz=timezone.utc).date().isoformat()
    except (TypeError, ValueError, OverflowError, OSError):
        try:
            return datetime.fromisoformat(
                str(value).replace('Z', '+00:00')).date().isoformat()
        except ValueError:
            return None


def _datetime_iso(value):
    if value in (None, ''):
        return None
    try:
        timestamp = int(float(value)) / 1000
        return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat()
    except (TypeError, ValueError, OverflowError, OSError):
        try:
            parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        except ValueError:
            return None
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.isoformat()


def _number(value, *, integer=False):
    if value in (None, ''):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return int(number) if integer else number


def _people(value):
    if not isinstance(value, list):
        text = _text(value)
        return [text] if text else []
    result = []
    for item in value:
        text = _text(item)
        if text and text not in result:
            result.append(text)
    return result


def _safe_hash(value, prefix=''):
    normalized = str(value or '').strip().casefold()
    if not normalized:
        return None
    return prefix + hashlib.sha256(normalized.encode('utf-8')).hexdigest()


def full_snapshot(records):
    accounts = []
    invalid = 0
    account_refs = set()
    order_refs = set()
    duplicate_accounts = 0
    duplicate_orders = 0
    for record in records:
        fields = record.get('fields') if isinstance(record, dict) else None
        fields = fields if isinstance(fields, dict) else (
            record if isinstance(record, dict) else {})
        email = _text(fields.get('邮箱账号'))
        site = _text(fields.get('站点')).upper()
        account_ref = _safe_hash(email)
        if not account_ref or site not in {'US', 'MX'}:
            invalid += 1
            continue
        order_ref = _safe_hash(_text(fields.get('号商购买单号')), 'sha256:')
        if account_ref in account_refs:
            duplicate_accounts += 1
            continue
        if order_ref and order_ref in order_refs:
            duplicate_orders += 1
            continue
        account_refs.add(account_ref)
        if order_ref:
            order_refs.add(order_ref)
        raw_status = _text(fields.get('账号状态'))
        if raw_status in AVAILABLE_STATUSES:
            availability = 'available'
        elif raw_status in DISABLED_STATUSES:
            availability = 'disabled'
        else:
            availability = 'manual_review'
        password = _text(fields.get('密码'))
        cookie = _text(fields.get('Cookie'))
        credential = 'ready' if password and cookie else 'unverified'
        if credential == 'invalid' and availability == 'available':
            availability = 'manual_review'
        account = {
            'accountRef': account_ref,
            'displayLabel': mask_email(email),
            'site': site,
            'availabilityStatus': availability,
            'credentialStatus': credential,
            'sourceStatus': raw_status or '旧台账迁移',
            'sourceVendorLabel': _text(fields.get('号商名称'))[:100],
            'sourceBatchRef': _text(fields.get('入库批次'))[:128],
            'operatorLabel': _text(fields.get('采购员'))[:100],
            'credentials': {
                'accountIdentifier': email,
                'password': password,
                'cookie': cookie,
                'verificationKeyLink': _text(fields.get('接码Key链接')),
            },
            'businessProfile': {
                'bindingEnvironment': _text(fields.get('绑定环境'))[:255],
                'abnormalRecord': _text(fields.get('异常记录'))[:20000],
                'note': _text(fields.get('备注'))[:20000],
                'sourcePurchaseOrderNo': _text(
                    fields.get('号商购买单号'))[:255],
                'sourceOperators': _people(fields.get('操作人'))[:50],
                'environmentSequence': _number(
                    fields.get('环境序号'), integer=True),
                'environmentGroupName': _text(
                    fields.get('环境分组名'))[:255],
                'sourceAccountId': _text(fields.get('账号ID'))[:128],
                'cumulativeOrderCount': _number(fields.get('累计下单数')),
                'migrationStatus': _text(fields.get('迁移状态'))[:100],
                'sourceCreatedBy': _text(fields.get('创建人'))[:255],
            },
        }
        for source_field, target_field in (
                ('绑定时间', 'bindingTime'),
                ('首次登录日期', 'firstLoginAt'),
                ('最后使用日期', 'lastUsedAt'),
                ('创建时间', 'sourceCreatedAt')):
            value = _datetime_iso(fields.get(source_field))
            if value:
                account['businessProfile'][target_field] = value
        purchase_date = _purchase_date(fields.get('购买日期'))
        if purchase_date:
            account['sourcePurchaseDate'] = purchase_date
        if order_ref:
            account['sourceOrderRef'] = order_ref
        # The legacy environment name has no guaranteed stable Hub reference,
        # so it is not imported as a partial environment pair.
        accounts.append(account)
    return {
        'accounts': accounts,
        'invalidCount': invalid,
        'duplicateAccountCount': duplicate_accounts,
        'duplicateOrderCount': duplicate_orders,
    }


def _load_ndjson(path):
    records = []
    with path.open(encoding='utf-8') as stream:
        for line in stream:
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise RuntimeError('旧表 NDJSON 包含无效记录')
            records.append(value)
    return records


def _load_config(path):
    with path.open(encoding='utf-8') as stream:
        value = json.load(stream)
    if not isinstance(value, dict):
        raise RuntimeError('config.json 格式无效')
    return value


def main(argv=None):
    parser = argparse.ArgumentParser(
        description='一次性迁移旧飞书表的完整买家号与凭证')
    parser.add_argument('--config', default='config.json')
    parser.add_argument('--records-ndjson')
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--confirm-full-credential-migration', action='store_true')
    args = parser.parse_args(argv)
    if args.apply and not args.confirm_full_credential_migration:
        parser.error('--apply 必须同时提供 --confirm-full-credential-migration')

    if args.records_ndjson:
        records = _load_ndjson(Path(args.records_ndjson).expanduser().resolve())
    else:
        config = _load_config(Path(args.config).expanduser().resolve())
        service = build_buyer_ledger_service(
            config, system_credential_store())
        metadata = service.client.get_target_metadata()
        table_name = str(metadata.get('table_name') or '').strip()
        if table_name != LEGACY_TABLE_NAME:
            raise RuntimeError('拒绝迁移：当前配置目标不是旧「买家号（统一）」表')
        records = service.client.list_records(list(SOURCE_FIELDS))
    result = full_snapshot(records)
    if result['invalidCount'] or result['duplicateAccountCount'] or result['duplicateOrderCount']:
        raise RuntimeError(
            '旧表完整数据存在异常：无效 %d、账号重复 %d、单号重复 %d；未写数据库' % (
                result['invalidCount'], result['duplicateAccountCount'],
                result['duplicateOrderCount']))
    accounts = result['accounts']
    if not accounts or len(accounts) > 500:
        raise RuntimeError('完整记录数量必须在 1-500 之间')
    print('旧表完整字段读取完成：%d 条记录；凭证内容未输出。' % len(accounts))
    if not args.apply:
        print('当前为 dry-run，未写数据库或新 Base。')
        return 0

    auth = LocalAuthService()
    totals = {'receivedCount': 0, 'createdCount': 0, 'updatedCount': 0,
              'unchangedCount': 0}
    for offset in range(0, len(accounts), 40):
        batch = accounts[offset:offset + 40]
        material = '\n'.join(sorted(item['accountRef'] for item in batch))
        snapshot_key = 'legacy-full-%03d-' % (offset // 40 + 1) + hashlib.sha256(
            material.encode('utf-8')).hexdigest()
        response = auth.buyer_account_request(
            '/v1/resources/buyer-accounts/snapshot', method='PUT',
            permission='resource.buyer.import', payload={
                'source': 'legacy_feishu_migration',
                'snapshotKey': snapshot_key,
                'accounts': batch,
            })
        data = response.get('data') if isinstance(response, dict) else None
        if not isinstance(data, dict):
            raise RuntimeError('云端买家号迁移响应无效')
        for key in totals:
            totals[key] += int(data.get(key) or 0)
    print(
        '数据库迁移完成：接收 %(receivedCount)s，新增 %(createdCount)s，'
        '更新 %(updatedCount)s，未变化 %(unchangedCount)s；Base 由 outbox 异步同步。'
        % totals)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
