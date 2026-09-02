# -*- coding: utf-8 -*-
"""Secure local storage for the optional HubStudio Local API key."""
import base64
import binascii
import os
from pathlib import Path
import sys

from .cloud_auth import (
    LocalAuthError,
    MacKeychainAuthSessionStore,
    WindowsDpapiAuthSessionStore,
)


KEYCHAIN_SERVICE = 'io.xynigo.sourcing.hubstudio-local-api'
KEYCHAIN_ACCOUNT = 'xynigo-hubstudio-local-api-key'


class HubApiKeyStoreError(RuntimeError):
    pass


def mask_hub_api_key(value):
    value = str(value or '').strip()
    if not value:
        return ''
    return '••••' + value[-4:] if len(value) > 4 else '••••'


def public_hub_api_key_status(store):
    """Return only configured state and a non-reusable recognition hint."""
    try:
        value = store.load() if store is not None else None
    except HubApiKeyStoreError:
        return {
            'hubApiKeyConfigured': False,
            'hubApiKeyMasked': '',
            'hubApiKeyStatus': 'unavailable',
        }
    return {
        'hubApiKeyConfigured': value is not None,
        'hubApiKeyMasked': mask_hub_api_key(value),
        'hubApiKeyStatus': 'configured' if value is not None else 'missing',
    }


def _validated_key(value):
    key = str(value or '').strip()
    # The reused secure-store backend accepts at most 256 characters. 180 raw
    # UTF-8 bytes remain below that limit after the local envelope/base64 step.
    if (not key or len(key.encode('utf-8')) > 180
            or '\x00' in key or '\n' in key or '\r' in key):
        raise HubApiKeyStoreError('HubStudio Local API Key 格式无效')
    return key


def _wrap_key(value):
    raw = _validated_key(value).encode('utf-8')
    encoded = base64.urlsafe_b64encode(raw).decode('ascii').rstrip('=')
    prefix = 'hubkey_%04d_' % len(encoded)
    return (prefix + encoded).ljust(32, 'x')


def _unwrap_key(value):
    wrapped = str(value or '')
    if not wrapped.startswith('hubkey_') or len(wrapped) < 12:
        raise HubApiKeyStoreError('HubStudio Local API Key 安全存储已损坏')
    try:
        length = int(wrapped[7:11])
        if wrapped[11] != '_' or length < 2:
            raise ValueError
        encoded = wrapped[12:12 + length]
        raw = base64.urlsafe_b64decode(
            encoded + '=' * ((4 - len(encoded) % 4) % 4))
        return _validated_key(raw.decode('utf-8'))
    except (TypeError, ValueError, UnicodeError, binascii.Error) as exc:
        raise HubApiKeyStoreError(
            'HubStudio Local API Key 安全存储已损坏') from exc


class MemoryHubApiKeyStore(object):
    def __init__(self, value=None):
        self.value = value

    def load(self):
        return _validated_key(self.value) if self.value else None

    def save(self, value):
        self.value = _validated_key(value)

    def clear(self):
        self.value = None


class SystemHubApiKeyStore(object):
    def __init__(self, backend):
        self.backend = backend

    def load(self):
        try:
            wrapped = self.backend.load()
            return _unwrap_key(wrapped) if wrapped else None
        except (LocalAuthError, HubApiKeyStoreError) as exc:
            raise HubApiKeyStoreError(
                '无法读取 HubStudio Local API Key') from exc

    def save(self, value):
        try:
            self.backend.save(_wrap_key(value))
        except LocalAuthError as exc:
            raise HubApiKeyStoreError(
                '无法保存 HubStudio Local API Key') from exc

    def clear(self):
        try:
            self.backend.clear()
        except LocalAuthError as exc:
            raise HubApiKeyStoreError(
                '无法清除 HubStudio Local API Key') from exc


def default_windows_hub_api_key_path():
    base = os.environ.get('LOCALAPPDATA')
    if not base:
        raise HubApiKeyStoreError('Windows 缺少 LOCALAPPDATA')
    return Path(base) / 'Xynigo' / 'credentials' / 'hubstudio-local-api.bin'


def system_hub_api_key_store():
    if sys.platform == 'darwin':
        return SystemHubApiKeyStore(MacKeychainAuthSessionStore(
            account=KEYCHAIN_ACCOUNT, service=KEYCHAIN_SERVICE))
    if os.name == 'nt':
        return SystemHubApiKeyStore(WindowsDpapiAuthSessionStore(
            path=default_windows_hub_api_key_path()))
    return MemoryHubApiKeyStore()
