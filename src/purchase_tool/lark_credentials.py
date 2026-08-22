# -*- coding: utf-8 -*-
"""Store the Feishu custom-app credential outside the public project.

macOS uses the login Keychain through the non-interactive ``security -i``
command channel without placing the secret in argv.  Windows stores a
CurrentUser-DPAPI encrypted blob below LOCALAPPDATA.
Only the App ID mask/configured state is exposed to the Web UI.
"""
from dataclasses import dataclass
import ctypes
import ctypes.wintypes
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile


KEYCHAIN_SERVICE = 'io.xynigo.sourcing.feishu'
KEYCHAIN_ACCOUNT = 'xynigo-lark-openapi'


class LarkCredentialError(Exception):
    pass


@dataclass(frozen=True)
class LarkCredentials:
    app_id: str
    app_secret: str

    def __post_init__(self):
        if not str(self.app_id or '').strip().startswith('cli_'):
            raise LarkCredentialError('飞书 App ID 格式无效')
        if len(str(self.app_secret or '').strip()) < 8:
            raise LarkCredentialError('飞书 App Secret 格式无效')

    def as_payload(self):
        return {'app_id': self.app_id.strip(),
                'app_secret': self.app_secret.strip()}


def _credentials_from_payload(payload):
    if not isinstance(payload, dict):
        raise LarkCredentialError('飞书应用凭证存储已损坏')
    return LarkCredentials(
        str(payload.get('app_id') or ''),
        str(payload.get('app_secret') or ''))


def mask_app_id(value):
    value = str(value or '')
    if len(value) <= 10:
        return '***' if value else ''
    return value[:7] + '***' + value[-4:]


class MemoryCredentialStore(object):
    """In-memory implementation used only by tests/FakeLark wiring."""

    def __init__(self, credentials=None):
        self.credentials = credentials

    def load(self):
        return self.credentials

    def save(self, app_id, app_secret):
        self.credentials = LarkCredentials(app_id, app_secret)

    def clear(self):
        self.credentials = None


class MacKeychainCredentialStore(object):
    def __init__(self, runner=subprocess.run, security_bin='/usr/bin/security'):
        self.runner = runner
        self.security_bin = security_bin

    def load(self):
        proc = self.runner(
            [self.security_bin, 'find-generic-password',
             '-a', KEYCHAIN_ACCOUNT, '-s', KEYCHAIN_SERVICE, '-w'],
            capture_output=True, text=True)
        if proc.returncode != 0:
            return None
        try:
            payload = json.loads((proc.stdout or '').strip())
        except Exception as exc:
            raise LarkCredentialError('macOS 钥匙串中的飞书凭证已损坏') from exc
        return _credentials_from_payload(payload)

    def save(self, app_id, app_secret):
        credentials = LarkCredentials(app_id, app_secret)
        secret_payload = json.dumps(
            credentials.as_payload(), ensure_ascii=False,
            separators=(',', ':'))
        # Use the interactive command channel only as a non-interactive stdin
        # transport.  ``-X`` accepts UTF-8 bytes as hex and therefore avoids
        # both the ``-w`` confirmation prompt and shell quoting.  The secret
        # never appears in process argv, the public config, or logs.
        secret_hex = secret_payload.encode('utf-8').hex()
        command = (
            'add-generic-password -a %s -s %s -U -X %s\n' %
            (KEYCHAIN_ACCOUNT, KEYCHAIN_SERVICE, secret_hex))
        try:
            proc = self.runner(
                [self.security_bin, '-i'], input=command,
                capture_output=True, text=True, timeout=15)
        except subprocess.TimeoutExpired as exc:
            raise LarkCredentialError(
                '保存飞书应用凭证到 macOS 钥匙串超时') from exc
        if proc.returncode != 0:
            raise LarkCredentialError('无法保存飞书应用凭证到 macOS 钥匙串')

    def clear(self):
        proc = self.runner(
            [self.security_bin, 'delete-generic-password',
             '-a', KEYCHAIN_ACCOUNT, '-s', KEYCHAIN_SERVICE],
            capture_output=True, text=True)
        if proc.returncode not in (0, 44):
            # Missing items have varied return codes across macOS versions.
            error = (proc.stderr or '').casefold()
            if 'could not be found' not in error:
                raise LarkCredentialError('无法清除 macOS 钥匙串中的飞书凭证')


class _DataBlob(ctypes.Structure):
    _fields_ = [('cbData', ctypes.wintypes.DWORD),
                ('pbData', ctypes.POINTER(ctypes.c_byte))]


def _as_blob(data):
    buffer = ctypes.create_string_buffer(data)
    return _DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte))), buffer


def _dpapi_protect(data):
    if os.name != 'nt':
        raise LarkCredentialError('Windows DPAPI 只能在 Windows 使用')
    source, source_buffer = _as_blob(data)
    target = _DataBlob()
    crypt32 = ctypes.windll.crypt32
    crypt32.CryptProtectData.argtypes = [
        ctypes.POINTER(_DataBlob), ctypes.wintypes.LPCWSTR,
        ctypes.POINTER(_DataBlob), ctypes.c_void_p, ctypes.c_void_p,
        ctypes.wintypes.DWORD, ctypes.POINTER(_DataBlob),
    ]
    crypt32.CryptProtectData.restype = ctypes.wintypes.BOOL
    kernel32 = ctypes.windll.kernel32
    kernel32.LocalFree.argtypes = [ctypes.wintypes.HLOCAL]
    kernel32.LocalFree.restype = ctypes.wintypes.HLOCAL
    if not crypt32.CryptProtectData(
            ctypes.byref(source), 'Xynigo Feishu credential', None, None,
            None, 0x01, ctypes.byref(target)):
        raise LarkCredentialError('Windows DPAPI 加密飞书凭证失败')
    try:
        return ctypes.string_at(target.pbData, target.cbData)
    finally:
        kernel32.LocalFree(ctypes.cast(
            target.pbData, ctypes.wintypes.HLOCAL))
        del source_buffer


def _dpapi_unprotect(data):
    if os.name != 'nt':
        raise LarkCredentialError('Windows DPAPI 只能在 Windows 使用')
    source, source_buffer = _as_blob(data)
    target = _DataBlob()
    crypt32 = ctypes.windll.crypt32
    crypt32.CryptUnprotectData.argtypes = [
        ctypes.POINTER(_DataBlob),
        ctypes.POINTER(ctypes.wintypes.LPWSTR),
        ctypes.POINTER(_DataBlob), ctypes.c_void_p, ctypes.c_void_p,
        ctypes.wintypes.DWORD, ctypes.POINTER(_DataBlob),
    ]
    crypt32.CryptUnprotectData.restype = ctypes.wintypes.BOOL
    kernel32 = ctypes.windll.kernel32
    kernel32.LocalFree.argtypes = [ctypes.wintypes.HLOCAL]
    kernel32.LocalFree.restype = ctypes.wintypes.HLOCAL
    if not crypt32.CryptUnprotectData(
            ctypes.byref(source), None, None, None, None, 0x01,
            ctypes.byref(target)):
        raise LarkCredentialError('Windows DPAPI 解密飞书凭证失败')
    try:
        return ctypes.string_at(target.pbData, target.cbData)
    finally:
        kernel32.LocalFree(ctypes.cast(
            target.pbData, ctypes.wintypes.HLOCAL))
        del source_buffer


def default_windows_credential_path():
    base = os.environ.get('LOCALAPPDATA')
    if not base:
        raise LarkCredentialError('Windows 缺少 LOCALAPPDATA，无法保存飞书凭证')
    return Path(base) / 'Xynigo' / 'credentials' / 'feishu-openapi.bin'


class WindowsDpapiCredentialStore(object):
    def __init__(self, path=None, protect_fn=_dpapi_protect,
                 unprotect_fn=_dpapi_unprotect):
        self.path = Path(path) if path else default_windows_credential_path()
        self.protect = protect_fn
        self.unprotect = unprotect_fn

    def load(self):
        if not self.path.is_file():
            return None
        try:
            plain = self.unprotect(self.path.read_bytes())
            payload = json.loads(plain.decode('utf-8'))
        except LarkCredentialError:
            raise
        except Exception as exc:
            raise LarkCredentialError('Windows 中的飞书应用凭证已损坏') from exc
        return _credentials_from_payload(payload)

    def save(self, app_id, app_secret):
        credentials = LarkCredentials(app_id, app_secret)
        plain = json.dumps(
            credentials.as_payload(), ensure_ascii=False,
            separators=(',', ':')).encode('utf-8')
        encrypted = self.protect(plain)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(
            prefix='.feishu-', suffix='.tmp', dir=str(self.path.parent))
        try:
            try:
                os.fchmod(fd, 0o600)
            except (AttributeError, OSError):
                pass
            with os.fdopen(fd, 'wb') as handle:
                handle.write(encrypted)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
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

    def clear(self):
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass


class UnsupportedCredentialStore(object):
    def load(self):
        return None

    def save(self, app_id, app_secret):
        del app_id, app_secret
        raise LarkCredentialError('当前系统不支持安全保存飞书应用凭证')

    def clear(self):
        return None


def system_credential_store():
    if sys.platform == 'darwin':
        return MacKeychainCredentialStore()
    if os.name == 'nt':
        return WindowsDpapiCredentialStore()
    return UnsupportedCredentialStore()


def public_credential_status(store):
    credentials = store.load()
    return {
        'credentialConfigured': credentials is not None,
        'appIdMasked': mask_app_id(credentials.app_id) if credentials else '',
    }
