# -*- coding: utf-8 -*-
"""Dynamic ordinary Feishu Sheet access for procurement image backfill.

The adapter deliberately uses the locally authenticated ``lark-cli`` user
identity.  It never reads or persists an access token and accepts only an
official ordinary Sheet URL.  Callers remain responsible for validating the
business header contract and for limiting writes to explicit cells.
"""
from dataclasses import dataclass
import csv
from io import StringIO
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time
from urllib.parse import urlparse

from .redaction import scrub_text


SHEET_TOKEN_RE = re.compile(r'^[A-Za-z0-9_-]{8,128}$')
SHEET_ID_RE = re.compile(r'^[A-Za-z0-9_-]{1,128}$')
ALLOWED_SHEET_HOSTS = ('feishu.cn', 'larksuite.com')
ROW_PREFIX_RE = re.compile(r'^\[row=(\d+)\]\s?')


class LarkSheetSyncError(ValueError):
    pass


def _column_name(index):
    result = ''
    value = int(index)
    while value > 0:
        value, remainder = divmod(value - 1, 26)
        result = chr(65 + remainder) + result
    return result


@dataclass(frozen=True)
class LarkSheetReference:
    url: str
    spreadsheet_token: str
    hostname: str


@dataclass(frozen=True)
class SheetTable:
    headers: tuple
    rows: tuple
    revision: object = None


def parse_lark_sheet_url(value):
    """Validate and normalize one ordinary Feishu/Lark Sheet URL."""
    source = str(value or '').strip()
    try:
        parsed = urlparse(source)
    except ValueError as exc:
        raise LarkSheetSyncError('飞书电子表格链接格式无效') from exc
    hostname = str(parsed.hostname or '').casefold().rstrip('.')
    if (parsed.scheme != 'https' or not hostname
            or not any(hostname == suffix or hostname.endswith('.' + suffix)
                       for suffix in ALLOWED_SHEET_HOSTS)):
        raise LarkSheetSyncError('只接受飞书或 Lark 官方 HTTPS 电子表格链接')
    parts = [item for item in parsed.path.split('/') if item]
    if len(parts) != 2 or parts[0] != 'sheets':
        raise LarkSheetSyncError('请粘贴 /sheets/ 类型的普通飞书电子表格链接')
    token = parts[1]
    if not SHEET_TOKEN_RE.fullmatch(token):
        raise LarkSheetSyncError('飞书电子表格标识格式无效')
    normalized = 'https://%s/sheets/%s' % (hostname, token)
    return LarkSheetReference(
        url=normalized, spreadsheet_token=token, hostname=hostname)


def normalize_sheet_id(value):
    sheet_id = str(value or '').strip()
    if not SHEET_ID_RE.fullmatch(sheet_id):
        raise LarkSheetSyncError('飞书工作表标识格式无效')
    return sheet_id


def _safe_cli_env():
    return dict(
        os.environ,
        LARKSUITE_CLI_NO_UPDATE_NOTIFIER='1',
        LARKSUITE_CLI_NO_SKILLS_NOTIFIER='1',
    )


def _parse_annotated_csv(value):
    """Return ``(real_row_number, values)`` without ever counting lines."""
    source = str(value or '')
    if not source:
        return ()
    logical_lines = []
    current_row = None
    current_parts = []
    for physical_line in source.splitlines(keepends=True):
        matched = ROW_PREFIX_RE.match(physical_line)
        if matched:
            if current_row is not None:
                logical_lines.append((current_row, ''.join(current_parts)))
            current_row = int(matched.group(1))
            current_parts = [physical_line[matched.end():]]
        elif current_row is not None:
            current_parts.append(physical_line)
    if current_row is not None:
        logical_lines.append((current_row, ''.join(current_parts)))
    result = []
    for row_number, csv_line in logical_lines:
        parsed = list(csv.reader(StringIO(csv_line)))
        if not parsed:
            values = []
        elif len(parsed) != 1:
            raise LarkSheetSyncError('飞书表格返回了无法定位的多行单元格')
        else:
            values = parsed[0]
        result.append((row_number, tuple(values)))
    return tuple(result)


def _cell_contains_image(value):
    """Detect rich embedded image payloads while ignoring plain image labels."""
    if isinstance(value, dict):
        for key, child in value.items():
            normalized = str(key or '').replace('_', '-').casefold()
            if normalized in {'file-token', 'image-token'} and child:
                return True
            if normalized in {'type', 'kind'} and 'image' in str(child).casefold():
                return True
            if _cell_contains_image(child):
                return True
        return False
    if isinstance(value, (list, tuple)):
        return any(_cell_contains_image(item) for item in value)
    if isinstance(value, str):
        normalized = value.strip().replace('_', '-').casefold()
        return normalized in {'embed-image', 'embedded-image', '[image]'}
    return False


def _cell_contains_link(value, expected_url=''):
    """Detect a rich-text hyperlink, optionally matching one exact URL."""
    wanted = str(expected_url or '').strip()
    if isinstance(value, dict):
        normalized = {
            str(key or '').replace('_', '-').casefold(): child
            for key, child in value.items()
        }
        link = str(normalized.get('link') or '').strip()
        item_type = str(normalized.get('type') or '').casefold()
        if link and (item_type == 'link' or 'link' in normalized):
            return not wanted or link == wanted
        return any(_cell_contains_link(child, wanted)
                   for child in value.values())
    if isinstance(value, (list, tuple)):
        return any(_cell_contains_link(item, wanted) for item in value)
    return False


def _cell_background_color(value):
    """Return one normalized ``#RRGGBB`` background color when present."""
    if isinstance(value, dict):
        for key, child in value.items():
            normalized = str(key or '').replace('_', '-').casefold()
            if normalized == 'background-color':
                color = str(child or '').strip().upper()
                if re.fullmatch(r'#[0-9A-F]{6}', color):
                    return color
                if re.fullmatch(r'[0-9A-F]{6}', color):
                    return '#' + color
            found = _cell_background_color(child)
            if found:
                return found
    elif isinstance(value, (list, tuple)):
        for item in value:
            found = _cell_background_color(item)
            if found:
                return found
    return ''


class LarkCliSheetsGateway(object):
    """Small credential-safe gateway around lark-cli Sheets shortcuts."""

    def __init__(self, lark_bin='lark-cli', runner=subprocess.run,
                 timeout=45, sleep_fn=time.sleep):
        self.lark_bin = str(lark_bin or 'lark-cli')
        self.runner = runner
        self.timeout = float(timeout)
        self.sleep = sleep_fn

    def _ensure_available(self):
        if self.runner is subprocess.run and not shutil.which(self.lark_bin):
            raise LarkSheetSyncError(
                '本机未安装 lark-cli，暂时无法读取或补齐飞书表格图片')

    def _run(self, command, url, sheet_id=None, extra=None, cwd=None):
        self._ensure_available()
        reference = parse_lark_sheet_url(url)
        argv = [
            self.lark_bin, 'sheets', command,
            '--url', reference.url,
            '--as', 'user', '--format', 'json',
        ]
        if sheet_id is not None:
            argv += ['--sheet-id', normalize_sheet_id(sheet_id)]
        argv += list(extra or ())
        try:
            proc = self.runner(
                argv, cwd=cwd, capture_output=True, text=True,
                timeout=self.timeout, env=_safe_cli_env())
        except subprocess.TimeoutExpired as exc:
            raise LarkSheetSyncError('飞书电子表格请求超时，请稍后重试') from exc
        except OSError as exc:
            raise LarkSheetSyncError('无法启动本机飞书表格连接组件') from exc
        raw = str(proc.stdout or proc.stderr or '').strip()
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError) as exc:
            raise LarkSheetSyncError('飞书表格连接组件返回了非 JSON 结果') from exc
        if proc.returncode != 0 or payload.get('ok') is not True:
            error = payload.get('error') or {}
            message = (
                error.get('message') or error.get('hint')
                or error.get('subtype') or '未知错误')
            raise LarkSheetSyncError(
                '%s失败：%s' % (self._command_label(command),
                              scrub_text(message)[:200]))
        data = payload.get('data')
        if not isinstance(data, dict):
            raise LarkSheetSyncError('飞书表格连接组件返回结构无效')
        return data

    @staticmethod
    def _command_label(command):
        return {
            '+workbook-info': '读取飞书工作簿',
            '+csv-get': '读取飞书数据行',
            '+cells-get': '读取飞书图片列',
            '+table-put': '追加飞书采购数据',
            '+styles-put': '设置飞书采购行样式',
            '+cells-set': '写入飞书采购链接',
            '+cells-set-image': '写入飞书单元格图片',
            '+batch-update': '规范飞书协作表结构',
        }.get(command, '飞书表格请求')

    def inspect(self, url):
        reference = parse_lark_sheet_url(url)
        data = self._run('+workbook-info', reference.url)
        sheets = []
        for item in data.get('sheets') or ():
            if not isinstance(item, dict) or item.get('resource_type') != 'sheet':
                continue
            sheet_id = normalize_sheet_id(item.get('sheet_id'))
            name = str(item.get('sheet_name') or '').strip()
            if not name:
                continue
            sheets.append({
                'sheetId': sheet_id,
                'sheetName': name,
                'rowCount': int(item.get('row_count') or 0),
                'columnCount': int(item.get('column_count') or 0),
                'hidden': bool(item.get('is_hidden')),
            })
        if not sheets:
            raise LarkSheetSyncError('该飞书电子表格没有可用工作表')
        return {
            'url': reference.url,
            'spreadsheetToken': reference.spreadsheet_token,
            'revision': data.get('revision'),
            'sheets': sheets,
        }

    def read_table(self, url, sheet_id):
        data = self._run(
            '+csv-get', url, sheet_id,
            ['--range', 'A:AZ', '--max-chars', '5000000'])
        if data.get('has_more'):
            raise LarkSheetSyncError(
                '目标工作表数据超过本次安全读取上限，请先归档历史行后重试')
        logical_rows = _parse_annotated_csv(data.get('annotated_csv'))
        if not logical_rows or logical_rows[0][0] != 1:
            raise LarkSheetSyncError('目标工作表缺少第 1 行表头')
        return SheetTable(
            headers=logical_rows[0][1],
            rows=tuple(logical_rows[1:]),
            revision=data.get('revision'))

    def append_table_rows(self, url, sheet_name, columns, rows,
                          dtypes=None, formats=None):
        """Append one typed block without exposing business rows in argv.

        ``+table-put`` owns row growth and writes numeric values as numbers.
        The payload lives in a short-lived private directory instead of the
        command line, keeping recipient/order data out of process listings.
        """
        name = str(sheet_name or '').strip()
        headers = [str(item or '').strip() for item in columns or ()]
        values = [list(item) for item in rows or ()]
        if not name:
            raise LarkSheetSyncError('飞书工作表名称为空')
        if not headers or any(not item for item in headers):
            raise LarkSheetSyncError('待写入的飞书表头不完整')
        if len(set(headers)) != len(headers):
            raise LarkSheetSyncError('待写入的飞书表头存在重复列')
        if not values:
            return {'skipped': True, 'reason': 'empty'}
        if any(len(row) != len(headers) for row in values):
            raise LarkSheetSyncError('待写入的飞书数据列数与表头不一致')
        payload = {
            'sheets': [{
                'name': name,
                'mode': 'append',
                'header': False,
                'allow_overwrite': False,
                'columns': headers,
                'data': values,
                'dtypes': dict(dtypes or {}),
                'formats': dict(formats or {}),
            }],
        }
        with tempfile.TemporaryDirectory(prefix='xynigo-sheet-rows-') as tmp:
            filename = 'table.json'
            Path(tmp, filename).write_text(
                json.dumps(payload, ensure_ascii=False, separators=(',', ':')),
                encoding='utf-8')
            return self._run(
                '+table-put', url,
                extra=['--sheets', '@./%s' % filename], cwd=tmp)

    @staticmethod
    def _cell_map(data, row_numbers, extractor):
        numbers = sorted({int(item) for item in row_numbers if int(item) >= 2})
        result = {row_number: extractor({}) for row_number in numbers}
        for item in data.get('ranges') or ():
            row_indices = item.get('row_indices') or ()
            cells = item.get('cells') or ()
            for index, row_number in enumerate(row_indices):
                number = int(row_number)
                if number not in result or index >= len(cells):
                    continue
                cell_row = cells[index]
                cell = cell_row[0] if isinstance(cell_row, list) and cell_row else {}
                result[number] = extractor(cell)
        return result

    def row_backgrounds(self, url, sheet_id, row_numbers):
        numbers = sorted({int(item) for item in row_numbers if int(item) >= 2})
        if not numbers:
            return {}
        data = self._run(
            '+cells-get', url, sheet_id,
            ['--range', 'A%d:A%d' % (numbers[0], numbers[-1]),
             '--include', 'style', '--max-chars', '5000000'])
        if data.get('has_more'):
            raise LarkSheetSyncError('目标采购行样式读取不完整，已停止写入')
        return self._cell_map(data, numbers, _cell_background_color)

    def apply_row_presentation(self, url, sheet_name, background_bands,
                               row_ranges, row_height=52, last_column='AH'):
        """Apply compact row height and order-group colors in safe chunks."""
        name = str(sheet_name or '').strip()
        last = str(last_column or '').strip().upper()
        if not name:
            raise LarkSheetSyncError('飞书工作表名称为空')
        if not re.fullmatch(r'[A-Z]{1,3}', last):
            raise LarkSheetSyncError('飞书采购协作表末列无效')
        bands = list(background_bands or ())
        ranges = list(row_ranges or ())
        results = []
        for offset in range(0, len(bands), 40):
            styles = [{
                'range': 'A%d:%s%d' % (
                    int(item['start']), last, int(item['end'])),
                'background_color': str(item['color']),
                'vertical_alignment': 'middle',
                'word_wrap': 'auto-wrap',
            } for item in bands[offset:offset + 40]]
            payload = {'styles': [{'name': name, 'cell_styles': styles}]}
            with tempfile.TemporaryDirectory(
                    prefix='xynigo-sheet-styles-') as tmp:
                filename = 'styles.json'
                Path(tmp, filename).write_text(
                    json.dumps(payload, ensure_ascii=False,
                               separators=(',', ':')), encoding='utf-8')
                results.append(self._run(
                    '+styles-put', url,
                    extra=['--styles', '@./%s' % filename], cwd=tmp))
        for offset in range(0, len(ranges), 80):
            sizes = [{
                'range': '%d:%d' % (int(item[0]), int(item[1])),
                'size': int(row_height),
            } for item in ranges[offset:offset + 80]]
            payload = {'styles': [{'name': name, 'row_sizes': sizes}]}
            with tempfile.TemporaryDirectory(
                    prefix='xynigo-sheet-row-heights-') as tmp:
                filename = 'row-heights.json'
                Path(tmp, filename).write_text(
                    json.dumps(payload, ensure_ascii=False,
                               separators=(',', ':')), encoding='utf-8')
                results.append(self._run(
                    '+styles-put', url,
                    extra=['--styles', '@./%s' % filename], cwd=tmp))
        return {'operations': len(results), 'skipped': not results}

    def normalize_collaboration_headers(self, url, sheet_id, sheet_name,
                                        headers, last_row=1, rows=()):
        """Normalize legacy business and receiver fields without data loss.

        The checkout area is deliberately untouched.  The structural chain is
        sent as one ordered high-risk batch after the caller's explicit sync
        confirmation.  A dry-run validates the whole ordered operation chain
        before it is executed, then the caller re-reads the whole header row.
        """
        actual = [str(item or '').strip() for item in headers or ()]
        operations = []
        aliases = {
            '分单标记': '分单日期', '分单时间': '分单日期',
            '采购单号': '包裹号', '销售金额': '销售订单金额',
            '平台订单号': '采购订单号',
            '收件人': '收货人姓名', '国家': '收货人国家',
            '收件地址': '地址1', '电话': '收货人电话',
        }
        receiver_fields = [
            '收货人姓名', '收货人国家', '收货人州/省', '收货人城市',
            '地址1', '地址2', '邮编', '收货人电话',
        ]
        legacy_receiver_fields = ['收件人', '国家', '收件地址', '邮编', '电话']
        for index, value in enumerate(actual, start=1):
            if value in {'分单标记', '分单时间', '采购单号',
                         '销售金额', '平台订单号'}:
                operations.append({
                    'shortcut': '+cells-set',
                    'input': {
                        'sheet_id': normalize_sheet_id(sheet_id),
                        'range': '%s1' % _column_name(index),
                        'cells': [[{'value': aliases[value]}]],
                    },
                })
        canonical = [aliases.get(value, value) for value in actual]
        # 分单批次与系统导入批次语义重复；只有每个非空值都已由
        # 导入批次等值覆盖时才允许删列，避免丢失唯一协作信息。
        if '分单批次' in actual:
            split_index = actual.index('分单批次')
            try:
                import_index = canonical.index('导入批次')
            except ValueError as exc:
                raise LarkSheetSyncError(
                    '目标表缺少导入批次，不能安全删除分单批次') from exc
            for row_number, raw in rows or ():
                padded = list(raw) + [''] * max(0, len(actual) - len(raw))
                split_value = str(padded[split_index] or '').strip()
                import_value = str(padded[import_index] or '').strip()
                if split_value and split_value != import_value:
                    raise LarkSheetSyncError(
                        '第 %d 行分单批次未被导入批次等值覆盖，已停止删列' %
                        int(row_number))
            column = _column_name(split_index + 1)
            operations.append({
                'shortcut': '+dim-delete',
                'input': {
                    'sheet_name': str(sheet_name or '').strip(),
                    'range': '%s:%s' % (column, column),
                },
            })
            actual.pop(split_index)
            canonical.pop(split_index)

        if '分单日期' not in canonical:
            column = 'A'
            operations.extend(({
                'shortcut': '+dim-insert',
                'input': {
                    'sheet_name': str(sheet_name or '').strip(),
                    'position': column, 'count': 1,
                    'inherit_style': 'before',
                },
            }, {
                'shortcut': '+cells-set',
                'input': {
                    'sheet_id': normalize_sheet_id(sheet_id),
                    'range': '%s1' % column,
                    'cells': [[{'value': '分单日期'}]],
                },
            }))
            canonical.insert(0, '分单日期')
            actual.insert(0, '分单日期')
        # 先插入明细金额，再以更新后的列坐标迁移收货信息。
        if '商品金额' not in canonical:
            try:
                amount_index = canonical.index('销售订单金额') + 1
            except ValueError as exc:
                raise LarkSheetSyncError('目标表缺少销售订单金额，无法安全插入商品金额') from exc
            column = _column_name(amount_index + 1)
            operations.extend(({
                'shortcut': '+dim-insert',
                'input': {
                    'sheet_name': str(sheet_name or '').strip(),
                    'position': column, 'count': 1,
                    'inherit_style': 'before',
                },
            }, {
                'shortcut': '+cells-set',
                'input': {
                    'sheet_id': normalize_sheet_id(sheet_id),
                    'range': '%s1' % column,
                    'cells': [[{'value': '商品金额'}]],
                },
            }))
            insert_at = canonical.index('销售订单金额') + 1
            canonical.insert(insert_at, '商品金额')
            actual.insert(insert_at, '商品金额')

        # 导入操作人属于系统追踪区，与采购员职责字段分开。
        if '导入操作人' not in canonical:
            try:
                operator_index = canonical.index('系统订单键') + 1
            except ValueError as exc:
                raise LarkSheetSyncError(
                    '目标表缺少系统订单键，无法安全插入导入操作人') from exc
            column = _column_name(operator_index + 1)
            operations.extend(({
                'shortcut': '+dim-insert',
                'input': {
                    'sheet_name': str(sheet_name or '').strip(),
                    'position': column, 'count': 1,
                    'inherit_style': 'before',
                },
            }, {
                'shortcut': '+cells-set',
                'input': {
                    'sheet_id': normalize_sheet_id(sheet_id),
                    'range': '%s1' % column,
                    'cells': [[{'value': '导入操作人'}]],
                },
            }))
            canonical.insert(operator_index, '导入操作人')
            actual.insert(operator_index, '导入操作人')

        guide_index = canonical.index('采购指导价')
        receiver_block = canonical[
            guide_index + 1:guide_index + 1 + len(receiver_fields)]
        if receiver_block != receiver_fields:
            legacy_positions = {
                name: index for index, name in enumerate(actual)
                if name in legacy_receiver_fields
            }
            if not legacy_positions:
                raise LarkSheetSyncError(
                    '目标表收货人字段不完整或位置错误，无法自动安全迁移')
            insert_index = guide_index + 1
            start_column = _column_name(insert_index + 1)
            end_column = _column_name(insert_index + len(receiver_fields))
            operations.extend(({
                'shortcut': '+dim-insert',
                'input': {
                    'sheet_name': str(sheet_name or '').strip(),
                    'position': start_column, 'count': len(receiver_fields),
                    'inherit_style': 'before',
                },
            }, {
                'shortcut': '+cells-set',
                'input': {
                    'sheet_id': normalize_sheet_id(sheet_id),
                    'range': '%s1:%s1' % (start_column, end_column),
                    'cells': [[{'value': value}
                               for value in receiver_fields]],
                },
            }))
            # 老表合并地址不做虚假拆分，完整保留到新“地址1”。
            value_mapping = {
                '收件人': '收货人姓名', '国家': '收货人国家',
                '收件地址': '地址1', '邮编': '邮编',
                '电话': '收货人电话',
            }
            bounded_last_row = max(1, int(last_row or 1))
            if bounded_last_row > 1:
                for legacy_name, target_name in value_mapping.items():
                    if legacy_name not in legacy_positions:
                        continue
                    source_column = _column_name(
                        legacy_positions[legacy_name] + 1)
                    target_column = _column_name(
                        insert_index + receiver_fields.index(target_name) + 1)
                    operations.append({
                        'shortcut': '+range-copy',
                        'input': {
                            'sheet_id': normalize_sheet_id(sheet_id),
                            'source_range': '%s2:%s%d' % (
                                source_column, source_column,
                                bounded_last_row),
                            'target_range': '%s2' % target_column,
                            'paste_type': 'values',
                        },
                    })
            # batch-update 的单个 dim-delete 子操作只接受一个 range。
            # 按列号从右到左删除，避免前面的删除改变后续坐标。
            for _name, index in sorted(
                    legacy_positions.items(), key=lambda item: item[1],
                    reverse=True):
                column = _column_name(index + 1)
                operations.append({
                    'shortcut': '+dim-delete',
                    'input': {
                        'sheet_name': str(sheet_name or '').strip(),
                        'range': '%s:%s' % (column, column),
                    },
                })
        if not operations:
            return {'operations': 0, 'skipped': True}
        with tempfile.TemporaryDirectory(
                prefix='xynigo-sheet-header-structure-') as tmp:
            filename = 'operations.json'
            Path(tmp, filename).write_text(
                json.dumps(operations, ensure_ascii=False,
                           separators=(',', ':')), encoding='utf-8')
            preview = self._run(
                '+batch-update', url,
                extra=['--dry-run', '--operations', '@./%s' % filename],
                cwd=tmp)
            result = self._run(
                '+batch-update', url,
                extra=['--yes', '--operations', '@./%s' % filename], cwd=tmp)
        return {
            'operations': len(operations), 'preview': preview,
            'result': result,
        }

    def reorder_collaboration_headers(self, url, sheet_id, sheet_name,
                                      headers, desired_headers):
        """Move demand columns into the standard order without rewriting cells."""
        actual = [str(item or '').strip() for item in headers or ()]
        aliases = {
            '分单标记': '分单日期', '分单时间': '分单日期',
            '采购单号': '包裹号', '销售金额': '销售订单金额',
            '平台订单号': '采购订单号',
            '收件人': '收货人姓名', '国家': '收货人国家',
            '收件地址': '地址1', '电话': '收货人电话',
        }
        canonical = [aliases.get(value, value) for value in actual]
        results = []
        for target_index, header in enumerate(tuple(desired_headers or ())):
            try:
                source_index = canonical.index(header)
            except ValueError as exc:
                raise LarkSheetSyncError(
                    '目标表缺少“%s”，无法规范采购需求区顺序' % header) from exc
            if source_index == target_index:
                continue
            source_column = _column_name(source_index + 1)
            target_column = _column_name(target_index + 1)
            results.append(self._run(
                '+dim-move', url, sheet_id,
                ['--source-range', '%s:%s' % (
                    source_column, source_column), '--target', target_column]))
            value = canonical.pop(source_index)
            canonical.insert(target_index, value)
        return {'operations': len(results), 'results': results}

    def normalize_date_column(self, url, sheet_name, headers, rows,
                              header='分单日期', number_format='yyyy-mm-dd'):
        """Rewrite existing non-empty date labels as true typed Sheet dates."""
        names = [str(item or '').strip() for item in headers or ()]
        try:
            column_index = names.index(str(header))
        except ValueError as exc:
            raise LarkSheetSyncError('目标表缺少分单日期') from exc
        parsed = []
        for row_number, raw in rows or ():
            value = raw[column_index] if column_index < len(raw) else ''
            text = str(value or '').strip()
            if not text:
                continue
            match = re.match(r'^(\d{4}-\d{2}-\d{2})', text)
            if not match:
                raise LarkSheetSyncError(
                    '第 %d 行分单日期不是有效日期，已停止类型转换' %
                    int(row_number))
            parsed.append((int(row_number), match.group(1)))
        if not parsed:
            return {'operations': 0, 'skipped': True}
        start = min(row for row, _value in parsed)
        end = max(row for row, _value in parsed)
        values = dict(parsed)
        payload = {'sheets': [{
            'name': str(sheet_name or '').strip(),
            'start_cell': '%s%d' % (_column_name(column_index + 1), start),
            'mode': 'overwrite',
            'header': False,
            'allow_overwrite': True,
            'columns': [str(header)],
            'data': [[values.get(row)] for row in range(start, end + 1)],
            'dtypes': {str(header): 'datetime64[ns]'},
            'formats': {str(header): str(number_format)},
        }]}
        with tempfile.TemporaryDirectory(
                prefix='xynigo-sheet-split-dates-') as tmp:
            filename = 'dates.json'
            Path(tmp, filename).write_text(
                json.dumps(payload, ensure_ascii=False,
                           separators=(',', ':')), encoding='utf-8')
            result = self._run(
                '+table-put', url,
                extra=['--sheets', '@./%s' % filename], cwd=tmp)
        return {'operations': 1, 'rows': len(parsed), 'result': result}

    def apply_header_presentation(self, url, sheet_id, sheet_name, zones):
        """Color the three header zones and attach collaboration notes."""
        name = str(sheet_name or '').strip()
        items = list(zones or ())
        if not name or not items:
            return {'operations': 0, 'skipped': True}
        styles = []
        writes = []
        for item in items:
            start = str(item['start']).upper()
            end = str(item['end']).upper()
            styles.append({
                'range': '%s1:%s1' % (start, end),
                'background_color': str(item['color']),
                'font_color': '#FFFFFF', 'font_weight': 'bold',
                'horizontal_alignment': 'center',
                'vertical_alignment': 'middle',
                'word_wrap': 'auto-wrap',
            })
            writes.append({
                'sheet_id': normalize_sheet_id(sheet_id),
                'range': '%s1' % start,
                'cells': [[{'note': str(item['note'])}]],
            })
        payload = {'styles': [{
            'name': name, 'cell_styles': styles,
            'row_sizes': [{'range': '1:1', 'size': 36}],
            'freeze': {'rows': 1, 'cols': 5},
        }]}
        results = []
        with tempfile.TemporaryDirectory(
                prefix='xynigo-sheet-header-styles-') as tmp:
            filename = 'styles.json'
            Path(tmp, filename).write_text(
                json.dumps(payload, ensure_ascii=False,
                           separators=(',', ':')), encoding='utf-8')
            results.append(self._run(
                '+styles-put', url,
                extra=['--styles', '@./%s' % filename], cwd=tmp))
        with tempfile.TemporaryDirectory(
                prefix='xynigo-sheet-header-notes-') as tmp:
            filename = 'notes.json'
            Path(tmp, filename).write_text(
                json.dumps(writes, ensure_ascii=False,
                           separators=(',', ':')), encoding='utf-8')
            results.append(self._run(
                '+cells-set', url,
                extra=['--writes', '@./%s' % filename], cwd=tmp))
        return {'operations': len(results)}

    def hyperlink_presence(self, url, sheet_id, expected_links,
                           column='N'):
        links = {
            int(row): str(link or '').strip()
            for row, link in dict(expected_links or {}).items()
            if int(row) >= 2 and str(link or '').strip()
        }
        if not links:
            return {}
        numbers = sorted(links)
        data = self._run(
            '+cells-get', url, sheet_id,
            ['--range', '%s%d:%s%d' % (
                column, numbers[0], column, numbers[-1]),
             '--include', 'value', '--max-chars', '5000000'])
        if data.get('has_more'):
            raise LarkSheetSyncError('目标采购链接列读取不完整，已停止写入')
        cells = self._cell_map(data, numbers, lambda value: value)
        return {
            row: _cell_contains_link(cells.get(row), links[row])
            for row in numbers
        }

    def set_hyperlinks(self, url, sheet_id, links, column='N'):
        """Turn system-owned purchase URLs into compact clickable labels."""
        items = [
            (int(row), str(link or '').strip()) for row, link in links or ()
            if int(row) >= 2 and str(link or '').strip()
        ]
        results = []
        for offset in range(0, len(items), 100):
            writes = [{
                'sheet_id': normalize_sheet_id(sheet_id),
                'range': '%s%d' % (column, row),
                'cells': [[{'rich_text': [{
                    'type': 'link', 'text': '打开采购链接', 'link': link,
                }]}]],
            } for row, link in items[offset:offset + 100]]
            with tempfile.TemporaryDirectory(
                    prefix='xynigo-sheet-links-') as tmp:
                filename = 'links.json'
                Path(tmp, filename).write_text(
                    json.dumps(writes, ensure_ascii=False,
                               separators=(',', ':')), encoding='utf-8')
                results.append(self._run(
                    '+cells-set', url,
                    extra=['--writes', '@./%s' % filename], cwd=tmp))
        return {'operations': len(results), 'skipped': not results}

    def image_presence(self, url, sheet_id, row_numbers, column='M'):
        numbers = sorted({int(item) for item in row_numbers if int(item) >= 2})
        if not numbers:
            return {}
        data = self._run(
            '+cells-get', url, sheet_id,
            ['--range', '%s%d:%s%d' % (
                column, numbers[0], column, numbers[-1]),
             '--include', 'value', '--max-chars', '5000000'])
        if data.get('has_more'):
            raise LarkSheetSyncError('目标图片列读取不完整，已停止写入')
        result = {row_number: False for row_number in numbers}
        for item in data.get('ranges') or ():
            row_indices = item.get('row_indices') or ()
            cells = item.get('cells') or ()
            for index, row_number in enumerate(row_indices):
                if int(row_number) not in result or index >= len(cells):
                    continue
                cell_row = cells[index]
                cell = cell_row[0] if isinstance(cell_row, list) and cell_row else {}
                result[int(row_number)] = _cell_contains_image(cell)
        return result

    def set_image(self, url, sheet_id, row_number, image_bytes, mime,
                  column='M'):
        row = int(row_number)
        if row < 2:
            raise LarkSheetSyncError('拒绝覆盖飞书表头图片单元格')
        suffix = {
            'image/jpeg': '.jpg', 'image/png': '.png', 'image/gif': '.gif',
            'image/webp': '.webp',
        }.get(str(mime or '').casefold())
        if not suffix:
            raise LarkSheetSyncError('订单商品图片格式不受飞书补图支持')
        with tempfile.TemporaryDirectory(prefix='xynigo-sheet-image-') as tmp:
            filename = 'order-image%s' % suffix
            Path(tmp, filename).write_bytes(bytes(image_bytes))
            return self._run(
                '+cells-set-image', url, sheet_id,
                ['--range', '%s%d' % (column, row), '--image', filename,
                 '--name', filename], cwd=tmp)

    def verify_image(self, url, sheet_id, row_number, column='M'):
        return bool(self.image_presence(
            url, sheet_id, [row_number], column=column).get(
            int(row_number)))
