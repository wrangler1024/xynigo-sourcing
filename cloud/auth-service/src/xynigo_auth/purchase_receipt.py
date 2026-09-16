"""Explicit, tenant-scoped purchase evidence writes. Never changes procurement state."""
from __future__ import annotations

import base64
import hashlib
import json
import re
import uuid
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from .models import PurchaseReceipt, PurchaseReceiptSlot, PurchaseReceiptOrder
from .procurement_import_sheet import _column_name, _cell_contains_image, LarkSheetSyncError


class ReceiptError(ValueError):
    pass


FILL_COLORS = ('', '#E2F0D9', '#DDEBF7', '#FFF2CC', '#FCE4D6', '#F4DCE6', '#E4DFEC', '#DDF2EF')


class ReceiptBody(BaseModel):
    model_config = ConfigDict(extra='forbid')
    sourceId: str = Field(min_length=1, max_length=100)
    taskKey: str = Field(pattern=r'^PT1-[0-9a-f]{64}$')
    action: Literal['preview', 'submit', 'retry-image', 'retry-color', 'status']
    requestId: str = Field(default='', max_length=64)
    expectedRevision: int = Field(default=0, ge=0)
    fingerprint: str = Field(default='', max_length=64)
    orderNo: str = Field(default='', max_length=64)
    amount: str = Field(default='', max_length=24)
    currency: Literal['MXN'] = 'MXN'
    image: str = Field(default='', max_length=4_000_000)
    reason: str = Field(default='', max_length=300)
    paidAt: str = Field(default='', max_length=40)
    fillColor: str = Field(default='', max_length=7)

    @field_validator('fillColor')
    @classmethod
    def fill_color(cls, value):
        if value not in FILL_COLORS:
            raise ValueError('请选择预设浅色或不填色')
        return value

    @field_validator('orderNo')
    @classmethod
    def order_no(cls, v):
        if v and not re.fullmatch(r'[A-Za-z0-9-]{6,64}', v):
            raise ValueError('采购订单号格式无效')
        return v


def sheet_url(target):
    return 'https://feishu.cn/sheets/' + target['spreadsheetToken']


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(',', ':')).encode()).hexdigest()


def key_for(row):
    source = str(row.get('系统订单键') or '').strip()
    sales = str(row.get('销售订单号') or '').strip()
    package = str(row.get('包裹号') or '').strip()
    values = [source or sales + '|' + package, str(row.get('店铺') or '').strip(), sales, package]
    return 'PT1-' + hashlib.sha256(json.dumps(values, ensure_ascii=False,
                                           separators=(',', ':')).encode()).hexdigest()


IDENTITY_FIELDS = ('系统订单键', '销售订单号', '店铺', '包裹号', '主规格', '次规格', '需求数量', '收货人国家')
REQUIRED = ('系统订单键', '销售订单号', '店铺', '包裹号', '采购订单号', '实际付款', '下单截图')


def locate(gateway, target, task_key, user, admin):
    table = gateway.read_table(sheet_url(target), target['sheetId'])
    headers = [str(x or '').strip() for x in table.headers]
    if any(headers.count(x) != 1 for x in REQUIRED):
        raise ReceiptError('协作表必需列缺失或重名，请管理员核对表头')
    matches = []
    for number, values in table.rows:
        row = dict(zip(headers, values))
        if key_for(row) == task_key:
            matches.append((number, row))
    if not matches:
        raise ReceiptError('原采购任务已不存在，请重新搜索；拆单后缀必须完整保留')
    # Money must be compared as a numeric cell, not a formatted currency string.
    column = _column_name(headers.index('实际付款') + 1)
    if hasattr(gateway, 'read_money_cells'):
        amounts = gateway.read_money_cells(sheet_url(target), target['sheetId'], column, [number for number, _ in matches])
        for (_, row), amount in zip(matches, amounts):
            row['实际付款'] = amount
    else:
        for number, row in matches:
            data = gateway._read_range(sheet_url(target), target['sheetId'],
                                       f'{column}{number}:{column}{number}', raw=True)
            cells = (data.get('valueRange') or {}).get('values') or []
            row['实际付款'] = cells[0][0] if cells and cells[0] else None
    fp = digest({'target': [target['spreadsheetToken'], target['sheetId']],
                 'headers': headers,
                 'rows': [{k: str(r.get(k) or '') for k in IDENTITY_FIELDS} for _, r in matches]})
    return headers, matches, fp


def public_receipt(record):
    color = record.image_cells.get('color') or {'selected': '', 'state': 'skipped'}
    result = {'ok': True, 'receiptId': str(record.id), 'revision': record.revision, 'requestId': record.request_id,
            'state': record.state, 'orderNo': record.order_no,
            'amount': record.amount, 'currency': record.currency,
            'message': {'complete': '采购详情已回传', 'image_failed': '订单号与金额已写入，请补传截图',
                        'pending': '结果尚未确认，请勿重复提交', 'uncertain': '写入结果未知，请管理员核对协作表；禁止自动重试',
                        'text_done': '订单号与金额已写入，截图结果待确认'}.get(record.state, '待核对')}
    result['color'] = color
    if record.state == 'complete' and color['state'] in {'failed', 'pending'}:
        result['message'] = '采购详情已回传，填色未完成，可仅重试填色'
    elif record.state == 'complete' and color['state'] == 'complete':
        result['message'] = '采购详情已回传并填色'
    return result


def values_equal(actual, expected):
    if expected is None:
        return actual in (None, '')
    if isinstance(expected, Decimal):
        try:
            return Decimal(str(actual)) == expected
        except Exception:
            return False
    return str(actual or '') == expected


def execute(session, body, *, user, target, gateway, admin=False):
    tenant_id = user.tenant_id
    # A target is resolved from the cloud registry, never accepted as an arbitrary URL.
    slot_key = digest([sheet_url(target), target['sheetId'], body.taskKey])
    slot = session.scalar(select(PurchaseReceiptSlot).where(
        PurchaseReceiptSlot.tenant_id == tenant_id,
        PurchaseReceiptSlot.task_hash == slot_key).with_for_update())
    record = session.get(PurchaseReceipt, slot.receipt_id) if slot and slot.receipt_id else None
    if body.action in {'status', 'retry-image', 'retry-color'}:
        if not record or record.request_id != body.requestId:
            raise ReceiptError('未找到当前提交记录')
        if record.actor_id != user.id and not admin:
            raise ReceiptError('不能操作他人的采购凭证')
        if body.action == 'status':
            return public_receipt(record)
    headers, matches, fingerprint = locate(gateway, target, body.taskKey, user, admin)
    if body.action == 'retry-color':
        if record.state != 'complete':
            raise ReceiptError('凭证未完成，不能填色')
        if fingerprint != record.fingerprint:
            raise ReceiptError('任务已变化，请重新核对填色范围')
        check_expected(matches, record, written=True, check_image=False)
        if digest(read_images(gateway, target, headers, matches)) != record.image_cells.get('after'):
            raise ReceiptError('协作表截图已变化，请核对原凭证')
        return finish_color(session, gateway, target, body.taskKey, user, admin, record,
                            locked_slot=slot, verified_matches=matches)
    if body.action == 'preview':
        return {'ok': True, 'features': {'fillColorV1': True}, 'expectedRevision': slot.revision if slot else 0,
                'fingerprint': fingerprint, 'rowCount': len(matches),
                'targetLabel': target['label'], 'sheetName': target['sheetName'],
                'existing': public_receipt(record) if record else None}
    if body.action == 'submit' and record and record.request_id == body.requestId:
        # Identity of a request is immutable, including its image.
        try:
            incoming_hash = hashlib.sha256(base64.b64decode(body.image, validate=True)).hexdigest()
        except ValueError:
            raise ReceiptError('同一提交编号的截图无效') from None
        if (record.order_no != body.orderNo or record.amount != body.amount or
                record.image_hash != incoming_hash or
                body.fillColor != (record.image_cells.get('color') or {}).get('selected', '')):
            raise ReceiptError('同一提交编号的内容不能改变')
        return public_receipt(record)
    if body.action == 'retry-image':
        if record.state != 'image_failed':
            return public_receipt(record)
        if fingerprint != record.fingerprint:
            raise ReceiptError('任务已变化，请停止补传并核对')
        check_expected(matches, record, written=True)
        record.state = 'text_done'
        session.commit()  # Durable in-flight state before another external write.
        return write_image(session, gateway, target, body.taskKey, user, admin, record)
    if (slot and slot.revision != body.expectedRevision) or (not slot and body.expectedRevision):
        raise ReceiptError('采购凭证已被更新，请重新读取')
    if fingerprint != body.fingerprint:
        raise ReceiptError('采购任务已变化，请重新读取并核对')
    if record:
        if record.state != 'complete':
            raise ReceiptError('上一笔回传未完成，请先核对或补传')
        if record.actor_id != user.id and not admin:
            raise ReceiptError('不能修订他人的采购凭证')
        if not body.reason.strip():
            raise ReceiptError('修订或重新下单必须填写原因，旧凭证将保留')
    check_expected(matches, record, written=bool(record))
    image_cells = read_images(gateway, target, headers, matches)
    if record:
        if digest(image_cells) != record.image_cells.get('after'):
            raise ReceiptError('协作表截图已被人工修改，请先核对原凭证')
    elif any(cell not in (None, '', []) for cell in image_cells):
        raise ReceiptError('协作表已有截图，请核对原记录')
    try:
        amount = Decimal(body.amount)
        if not amount.is_finite() or amount < 0 or amount > Decimal('10000000') or amount.as_tuple().exponent < -2:
            raise ValueError()
        image = base64.b64decode(body.image, validate=True)
        if not 100 <= len(image) <= 3_000_000 or not image.startswith(b'\xff\xd8\xff'):
            raise ValueError()
        uuid.UUID(body.requestId)
        if not body.orderNo:
            raise ValueError()
    except Exception:
        raise ReceiptError('付款金额、截图或提交编号无效') from None
    if not slot:
        slot = PurchaseReceiptSlot(tenant_id=tenant_id, task_hash=slot_key, revision=0)
        session.add(slot)
        try:
            session.flush()
        except IntegrityError:
            session.rollback()
            raise ReceiptError('另一笔提交正在处理，请重新读取') from None
    order_binding = session.get(PurchaseReceiptOrder, (tenant_id, body.orderNo))
    if order_binding and order_binding.task_hash != slot_key:
        raise ReceiptError('该采购订单已关联其他任务，不能重复计款')
    if not order_binding:
        session.add(PurchaseReceiptOrder(tenant_id=tenant_id, order_no=body.orderNo, task_hash=slot_key))
    revision = slot.revision + 1
    current = PurchaseReceipt(id=uuid.uuid4(), tenant_id=tenant_id, actor_id=user.id,
        request_id=body.requestId, revision=revision, state='pending', fingerprint=fingerprint,
        order_no=body.orderNo, amount=body.amount, currency=body.currency, paid_at=body.paidAt,
        reason=body.reason, image=image, image_hash=hashlib.sha256(image).hexdigest(),
        previous_id=record.id if record else None, image_cells={'before': digest(image_cells),
            'color': {'selected': body.fillColor, 'state': 'pending' if body.fillColor else 'skipped'}})
    session.add(current)
    slot.revision, slot.receipt_id = revision, current.id
    try:
        session.commit()  # Persist intent first. Crash/timeout may not initiate another write.
    except IntegrityError:
        session.rollback()
        raise ReceiptError('提交编号重复，请核对已有记录') from None
    try:
        # Re-resolve after claiming the slot; do not write using a search-cache row index.
        headers, matches, fp = locate(gateway, target, body.taskKey, user, admin)
        if fp != fingerprint:
            raise ReceiptError('任务在写入前发生变化')
        check_expected(matches, record, written=bool(record))
        ranges = []
        for i, (number, _) in enumerate(matches):
            for name, value in [('采购订单号', body.orderNo), ('实际付款', float(amount) if i == 0 else '')]:
                column = _column_name(headers.index(name) + 1)
                ranges.append({'range': f"{target['sheetId']}!{column}{number}:{column}{number}", 'values': [[value]]})
        gateway._write_value_ranges(sheet_url(target), ranges)
        _, after, fp = locate(gateway, target, body.taskKey, user, admin)
        if fp != fingerprint:
            raise ReceiptError('任务在写入期间发生变化')
        check_expected(after, current, written=True, check_image=False)
        current.state = 'text_done'
        session.commit()
        return write_image(session, gateway, target, body.taskKey, user, admin, current)
    except Exception:
        current.state = 'uncertain'
        session.commit()
        return public_receipt(current)


def check_expected(matches, record, *, written, check_image=True):
    for i, (_, row) in enumerate(matches):
        order = record.order_no if written else ''
        money = Decimal(record.amount) if written and i == 0 else None
        if not values_equal(row.get('采购订单号'), order) or not values_equal(row.get('实际付款'), money):
            raise ReceiptError('协作表已有不同订单号或金额，请核对；不会覆盖人工记录')
        if not written and check_image and row.get('下单截图') not in (None, ''):
            raise ReceiptError('协作表已有截图，请核对原记录')


def read_images(gateway, target, headers, matches):
    column = _column_name(headers.index('下单截图') + 1)
    if hasattr(gateway, 'read_image_cells'):
        return gateway.read_image_cells(sheet_url(target), target['sheetId'], column, [row for row, _ in matches])
    result = []
    for row, _ in matches:
        data = gateway._read_range(sheet_url(target), target['sheetId'], f'{column}{row}:{column}{row}', raw=True)
        values = (data.get('valueRange') or {}).get('values') or []
        result.append(values[0][0] if values and values[0] else None)
    return result


def write_image(session, gateway, target, task_key, user, admin, record):
    try:
        headers, matches, fp = locate(gateway, target, task_key, user, admin)
        if fp != record.fingerprint:
            raise ReceiptError('任务在截图写入前变化')
        check_expected(matches, record, written=True, check_image=False)
        if digest(read_images(gateway, target, headers, matches)) != record.image_cells['before']:
            raise ReceiptError('截图单元格已变化，停止覆盖')
        row = matches[0][0]
        column = _column_name(headers.index('下单截图') + 1)
        gateway.set_image(sheet_url(target), target['sheetId'], row,
                          record.image, 'image/jpeg', column=column)
        headers, matches, fp = locate(gateway, target, task_key, user, admin)
        if fp != record.fingerprint:
            raise ReceiptError('任务在截图写入期间变化')
        check_expected(matches, record, written=True, check_image=False)
        cells = read_images(gateway, target, headers, matches)
        if not cells or not _cell_contains_image(cells[0]):
            raise ReceiptError('图片写入后未能确认')
        record.image_cells = {**record.image_cells, 'after': digest(cells)}
        record.state = 'complete'
    except LarkSheetSyncError as exc:
        record.state = 'image_failed' if exc.code in {90204, 90213, 91403, 90218, 99991672} else 'uncertain'
    except Exception:
        record.state = 'uncertain'
    session.commit()  # Evidence completion survives any later color failure/crash.
    if record.state == 'complete':
        return finish_color(session, gateway, target, task_key, user, admin, record)
    return public_receipt(record)


def finish_color(session, gateway, target, task_key, user, admin, record, *, locked_slot=None, verified_matches=None):
    color = record.image_cells.get('color') or {}
    if not color.get('selected'):
        return public_receipt(record)
    slot = locked_slot or session.scalar(select(PurchaseReceiptSlot).where(
        PurchaseReceiptSlot.tenant_id == user.tenant_id,
        PurchaseReceiptSlot.task_hash == digest([sheet_url(target), target['sheetId'], task_key])
    ).with_for_update().execution_options(populate_existing=True))
    session.refresh(record)
    color = record.image_cells.get('color') or {}
    if color.get('state') in {'complete', 'superseded'}:
        return public_receipt(record)
    if slot is None or slot.receipt_id != record.id:
        record.image_cells = {**record.image_cells, 'color': {**color, 'state': 'superseded'}}
        session.commit()
        return public_receipt(record)
    try:
        if verified_matches is None:
            headers, matches, fp = locate(gateway, target, task_key, user, admin)
            if fp != record.fingerprint:
                raise ReceiptError('任务已变化，停止填色')
            check_expected(matches, record, written=True, check_image=False)
            if digest(read_images(gateway, target, headers, matches)) != record.image_cells.get('after'):
                raise ReceiptError('截图已变化，停止填色')
        else:
            matches = verified_matches
        apply_color(gateway, target, matches, record)
    except Exception:
        record.image_cells = {**record.image_cells, 'color': {**color, 'state': 'failed'}}
    session.commit()
    return public_receipt(record)


def apply_color(gateway, target, matches, record):
    """Independent cosmetic result. Never downgrades a verified receipt."""
    color = record.image_cells.get('color') or {'selected': '', 'state': 'skipped'}
    selected = color.get('selected', '')
    if not selected:
        return
    if selected not in FILL_COLORS:
        raise ReceiptError('填色配置无效')
    rows = sorted({int(number) for number, _ in matches})
    if not rows or rows[0] < 2:
        raise ReceiptError('禁止修改表头颜色')
    try:
        bands = []
        for number in rows:
            if bands and number == bands[-1][1] + 1:
                bands[-1][1] = number
            else:
                bands.append([number, number])
        gateway._style_ranges(sheet_url(target), [{
            'ranges': [f"{target['sheetId']}!A{first}:AR{last}" for first, last in bands],
            'style': {'backColor': selected},
        }])
        state = 'complete'
    except Exception:
        state = 'failed'
    record.image_cells = {**record.image_cells, 'color': {'selected': selected, 'state': state}}
