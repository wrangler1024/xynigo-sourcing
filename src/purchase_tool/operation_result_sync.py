# -*- coding: utf-8 -*-
"""Durable local outbox for real environment and logistics results."""

from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import threading
import time

from .cloud_auth import LocalAuthError


FORBIDDEN_KEYS = frozenset({
    'password', 'cookie', 'cookietext', 'keyurl', 'appsecret',
    'accesstoken', 'refreshtoken', 'sessiontoken',
})


def default_operation_outbox_path():
    if os.name == 'nt':
        base = os.environ.get('LOCALAPPDATA') or tempfile.gettempdir()
        return Path(base) / 'Xynigo' / 'operation-result-outbox.json'
    if os.sys.platform == 'darwin':
        return (Path.home() / 'Library' / 'Application Support' /
                'Xynigo' / 'operation-result-outbox.json')
    return (Path.home() / '.local' / 'state' / 'xynigo' /
            'operation-result-outbox.json')


def _validate_no_credentials(value):
    if isinstance(value, dict):
        for key, item in value.items():
            normalized = ''.join(ch for ch in str(key).casefold()
                                 if ch.isalnum())
            if normalized in FORBIDDEN_KEYS:
                raise ValueError('业务结果队列拒绝保存凭证字段')
            _validate_no_credentials(item)
    elif isinstance(value, list):
        for item in value:
            _validate_no_credentials(item)


class OperationResultSyncQueue(object):
    """At-least-once uploader; successful server ingestion is idempotent."""

    def __init__(self, sender, path=None, interval_seconds=30,
                 start_worker=True, clock=time.time):
        self.sender = sender
        self.path = Path(path or default_operation_outbox_path())
        self.interval_seconds = max(5, int(interval_seconds))
        self.clock = clock
        self.lock = threading.Lock()
        self.flush_lock = threading.Lock()
        self.stop_event = threading.Event()
        self.items = self._load()
        self.worker = None
        if start_worker:
            self.worker = threading.Thread(target=self._loop, daemon=True)
            self.worker.start()

    def _load(self):
        if not self.path.is_file():
            return []
        try:
            with self.path.open(encoding='utf-8') as handle:
                payload = json.load(handle)
        except Exception:
            return []
        items = payload.get('items') if isinstance(payload, dict) else None
        if not isinstance(items, list):
            return []
        safe = []
        for item in items:
            if not isinstance(item, dict):
                continue
            try:
                _validate_no_credentials(item.get('payload'))
            except ValueError:
                continue
            if (item.get('runKey') and item.get('endpoint')
                    and item.get('permission')
                    and isinstance(item.get('payload'), dict)):
                safe.append(item)
        return safe[-500:]

    def _persist_locked(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(
            prefix='.operation-outbox-', suffix='.tmp',
            dir=str(self.path.parent))
        try:
            try:
                os.fchmod(fd, 0o600)
            except (AttributeError, OSError):
                pass
            with os.fdopen(fd, 'w', encoding='utf-8') as handle:
                json.dump({'version': 1, 'items': self.items}, handle,
                          ensure_ascii=False, separators=(',', ':'))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
            try:
                os.chmod(self.path, 0o600)
            except OSError:
                pass
        except Exception:
            try:
                os.close(fd)
            except OSError:
                pass
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise

    def enqueue(self, endpoint, permission, payload):
        _validate_no_credentials(payload)
        run_key = str(payload.get('runKey') or '')
        if not run_key:
            raise ValueError('业务结果缺少 runKey')
        canonical = json.dumps(
            payload, ensure_ascii=False, separators=(',', ':'),
            sort_keys=True)
        with self.lock:
            existing = next((item for item in self.items
                             if item.get('endpoint') == endpoint
                             and item.get('runKey') == run_key), None)
            if existing:
                if existing.get('canonical') != canonical:
                    raise ValueError('同一任务标识的本机结果不一致')
                return False
            if len(self.items) >= 500:
                raise RuntimeError('本机业务结果待同步队列已满，请联系管理员')
            self.items.append({
                'runKey': run_key,
                'endpoint': str(endpoint),
                'permission': str(permission),
                'payload': payload,
                'canonical': canonical,
                'attemptCount': 0,
                'nextAttemptAt': 0,
                'lastErrorCode': '',
                'createdAt': int(self.clock()),
            })
            self._persist_locked()
        self.flush()
        return True

    def flush(self):
        if not self.flush_lock.acquire(blocking=False):
            return 0
        completed = 0
        try:
            while True:
                with self.lock:
                    now = self.clock()
                    item = next((entry for entry in self.items
                                 if float(entry.get('nextAttemptAt') or 0)
                                 <= now), None)
                    if item is None:
                        break
                    snapshot = dict(item)
                try:
                    self.sender(
                        snapshot['endpoint'], snapshot['payload'],
                        snapshot['permission'])
                except Exception as exc:
                    code = (exc.code if isinstance(exc, LocalAuthError)
                            else 'local_upload_failed')
                    with self.lock:
                        current = next((entry for entry in self.items
                                        if entry.get('endpoint') ==
                                        snapshot['endpoint']
                                        and entry.get('runKey') ==
                                        snapshot['runKey']), None)
                        if current is None:
                            continue
                        attempts = int(current.get('attemptCount') or 0) + 1
                        current['attemptCount'] = attempts
                        current['lastErrorCode'] = str(code)[:128]
                        current['nextAttemptAt'] = int(
                            self.clock() + min(900, 15 * (2 ** min(6, attempts - 1))))
                        self._persist_locked()
                    break
                else:
                    with self.lock:
                        self.items = [
                            entry for entry in self.items
                            if not (entry.get('endpoint') == snapshot['endpoint']
                                    and entry.get('runKey') == snapshot['runKey'])
                        ]
                        self._persist_locked()
                    completed += 1
        finally:
            self.flush_lock.release()
        return completed

    def snapshot(self):
        with self.lock:
            return {
                'pending': len(self.items),
                'rows': [{
                    'runKey': item.get('runKey'),
                    'attemptCount': int(item.get('attemptCount') or 0),
                    'lastErrorCode': item.get('lastErrorCode') or '',
                } for item in self.items],
            }

    def _loop(self):
        while not self.stop_event.wait(self.interval_seconds):
            self.flush()

    def close(self):
        self.stop_event.set()
        if self.worker:
            self.worker.join(timeout=2)
