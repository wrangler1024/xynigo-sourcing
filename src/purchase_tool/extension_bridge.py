# -*- coding: utf-8 -*-
"""Short-lived, user-approved bridge between Xynigo and its Chrome extension."""
from __future__ import annotations

import hmac
import re
import secrets
import threading
import time


EXTENSION_ID_RE = re.compile(r'^[a-p]{32}$')
BRIDGE_TOKEN_RE = re.compile(r'^[A-Za-z0-9_-]{32,256}$')


class ExtensionBridgeError(Exception):
    def __init__(self, code, message, status=400):
        self.code = str(code)
        self.status = int(status)
        super().__init__(str(message))


class ExtensionBridge(object):
    def __init__(self, clock=time.monotonic, request_ttl=300.0):
        self.clock = clock
        self.request_ttl = float(request_ttl)
        self.lock = threading.RLock()
        self.pending = {}
        self.tokens = {}

    @staticmethod
    def validate_client_id(value):
        client_id = str(value or '').strip().lower()
        if not EXTENSION_ID_RE.fullmatch(client_id):
            raise ExtensionBridgeError(
                'extension_client_invalid', '浏览器扩展标识无效', 400)
        return client_id

    @staticmethod
    def client_id_from_origin(origin):
        raw = str(origin or '').strip().lower()
        prefix = 'chrome-extension://'
        if not raw.startswith(prefix):
            raise ExtensionBridgeError(
                'extension_origin_forbidden', '只允许浏览器扩展连接', 403)
        client_id = raw[len(prefix):]
        if '/' in client_id or '?' in client_id or '#' in client_id:
            raise ExtensionBridgeError(
                'extension_origin_forbidden', '浏览器扩展来源无效', 403)
        return ExtensionBridge.validate_client_id(client_id)

    def request_pairing(self, client_id, version, origin):
        client_id = self.validate_client_id(client_id)
        if self.client_id_from_origin(origin) != client_id:
            raise ExtensionBridgeError(
                'extension_origin_forbidden', '扩展来源与标识不一致', 403)
        version = str(version or '').strip()[:64]
        now = self.clock()
        with self.lock:
            self._purge(now)
            self.pending[client_id] = {
                'version': version,
                'expiresAt': now + self.request_ttl,
            }
        return {
            'clientId': client_id,
            'clientVersion': version,
            'expiresIn': int(self.request_ttl),
        }

    def approve(self, client_id):
        client_id = self.validate_client_id(client_id)
        now = self.clock()
        with self.lock:
            self._purge(now)
            request = self.pending.pop(client_id, None)
            if request is None:
                raise ExtensionBridgeError(
                    'extension_pairing_expired', '插件连接请求不存在或已过期', 410)
            token = secrets.token_urlsafe(48)
            self.tokens[client_id] = token
        return {
            'clientId': client_id,
            'clientVersion': request['version'],
            'bridgeToken': token,
        }

    def authenticate(self, client_id, token, origin):
        client_id = self.validate_client_id(client_id)
        if self.client_id_from_origin(origin) != client_id:
            raise ExtensionBridgeError(
                'extension_origin_forbidden', '扩展来源与标识不一致', 403)
        token = str(token or '').strip()
        if not BRIDGE_TOKEN_RE.fullmatch(token):
            raise ExtensionBridgeError(
                'extension_authentication_required', '请重新连接 Xynigo', 401)
        with self.lock:
            expected = self.tokens.get(client_id)
        if not expected or not hmac.compare_digest(
                token.encode('utf-8'), expected.encode('utf-8')):
            raise ExtensionBridgeError(
                'extension_authentication_required', '插件连接已失效，请重新连接', 401)
        return client_id

    def revoke(self, client_id):
        client_id = self.validate_client_id(client_id)
        with self.lock:
            self.pending.pop(client_id, None)
            self.tokens.pop(client_id, None)

    def _purge(self, now):
        expired = [
            client_id for client_id, request in self.pending.items()
            if request['expiresAt'] <= now
        ]
        for client_id in expired:
            self.pending.pop(client_id, None)
