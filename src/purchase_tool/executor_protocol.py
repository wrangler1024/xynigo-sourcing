# -*- coding: utf-8 -*-
"""Validate and handle the low-risk ``xynigo://`` launcher protocol.

The protocol is deliberately not a business command channel.  It may only
start/wake the executor or consume the same short-lived pairing code that a
user could enter in the packaged pairing launcher.
"""
from __future__ import annotations

import re
import sys
from urllib.parse import parse_qs, urlparse


PAIR_CODE_PATTERN = re.compile(r'^[A-HJ-NP-Z2-9]{4}-?[A-HJ-NP-Z2-9]{4}$')


class ExecutorProtocolError(ValueError):
    pass


def parse_executor_protocol_uri(value):
    raw = str(value or '').strip()
    if len(raw) > 1024:
        raise ExecutorProtocolError('协议地址过长')
    parsed = urlparse(raw)
    if parsed.scheme.casefold() != 'xynigo':
        raise ExecutorProtocolError('协议类型无效')
    try:
        port = parsed.port
    except ValueError:
        raise ExecutorProtocolError('协议端口无效') from None
    if parsed.username or parsed.password or port or parsed.fragment:
        raise ExecutorProtocolError('协议地址包含不允许的部分')
    action = (parsed.netloc or parsed.path.strip('/')).casefold()
    if not action or (parsed.netloc and parsed.path not in ('', '/')):
        raise ExecutorProtocolError('协议动作无效')
    query = parse_qs(parsed.query, keep_blank_values=True, strict_parsing=False)
    if any(len(values) != 1 for values in query.values()):
        raise ExecutorProtocolError('协议参数不能重复')
    if action in ('start', 'wake'):
        if query:
            raise ExecutorProtocolError('当前启动协议不接受参数')
        return {'action': action}
    if action == 'pair':
        if set(query) != {'code'}:
            raise ExecutorProtocolError('配对协议必须只包含一次性配对码')
        code = (query.get('code') or [''])[0].upper()
        if not PAIR_CODE_PATTERN.fullmatch(code):
            raise ExecutorProtocolError('配对码格式无效')
        normalized = code.replace('-', '')
        return {'action': 'pair', 'pairingCode': normalized[:4] + '-' + normalized[4:]}
    raise ExecutorProtocolError('协议动作不受支持')


def protocol_cli(argv=None):
    args = list(argv or [])
    if len(args) != 1:
        print('协议启动失败：缺少唯一的 xynigo:// 地址', file=sys.stderr)
        return 2
    try:
        command = parse_executor_protocol_uri(args[0])
        if command['action'] == 'pair':
            from .executor_channel import pair_executor
            pair_executor(command['pairingCode'])
            print('设备配对成功，正在启动本地执行器。')
        # Importing bootstrap starts the normal executor process. The parsed
        # ticket/code is intentionally not forwarded into business handlers.
        from . import bootstrap  # noqa: F401
        return 0
    except ExecutorProtocolError as exc:
        print('协议启动失败：%s' % exc, file=sys.stderr)
        return 2
    except Exception as exc:
        code = getattr(exc, 'code', 'executor_protocol_failed')
        print('协议启动失败：%s（%s）' % (str(exc), code), file=sys.stderr)
        return 1
