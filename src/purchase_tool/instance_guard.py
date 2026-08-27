# -*- coding: utf-8 -*-
"""Single-instance guard for standard Windows and macOS executors."""
from __future__ import annotations

import ctypes
import errno
import os
from pathlib import Path
import sys


ERROR_ALREADY_EXISTS = 183
WINDOWS_MUTEX_NAME = 'Local\\XynigoSourcing.Executor'
MACOS_LOCK_FILENAME = 'executor.lock'


class ExecutorInstanceGuard(object):
    def __init__(self, acquired=True, handle=None, close_fn=None):
        self.acquired = bool(acquired)
        self.handle = handle
        self.close_fn = close_fn

    def close(self):
        handle = self.handle
        self.handle = None
        if handle and self.close_fn:
            self.close_fn(handle)


def default_macos_lock_path(environ=None):
    environ = os.environ if environ is None else environ
    data_dir = str(environ.get('XYNIGO_DATA_DIR') or '').strip()
    if data_dir:
        return Path(data_dir).expanduser() / MACOS_LOCK_FILENAME
    return (Path.home() / 'Library' / 'Application Support' /
            'XynigoSourcing' / MACOS_LOCK_FILENAME)


def _acquire_posix_file_guard(lock_path):
    import fcntl

    path = Path(lock_path).expanduser()
    created_parent = not path.parent.exists()
    path.parent.mkdir(parents=True, exist_ok=True)
    if created_parent:
        path.parent.chmod(0o700)
    handle = path.open('a+b')
    try:
        os.chmod(str(path), 0o600)
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        handle.close()
        if exc.errno in (errno.EACCES, errno.EAGAIN):
            return ExecutorInstanceGuard(False)
        raise

    def close_file(file_handle):
        try:
            fcntl.flock(file_handle.fileno(), fcntl.LOCK_UN)
        finally:
            file_handle.close()

    return ExecutorInstanceGuard(True, handle, close_file)


def acquire_executor_instance_guard(lock_path=None):
    if os.name != 'nt':
        if lock_path is not None or sys.platform == 'darwin':
            return _acquire_posix_file_guard(
                lock_path or default_macos_lock_path())
        return ExecutorInstanceGuard(True)
    kernel32 = ctypes.windll.kernel32
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int,
                                      ctypes.c_wchar_p]
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    kernel32.GetLastError.argtypes = []
    kernel32.GetLastError.restype = ctypes.c_ulong
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = ctypes.c_int
    handle = kernel32.CreateMutexW(None, 0, WINDOWS_MUTEX_NAME)
    if not handle:
        raise OSError('无法创建 Xynigo 单实例互斥锁')
    if kernel32.GetLastError() == ERROR_ALREADY_EXISTS:
        kernel32.CloseHandle(handle)
        return ExecutorInstanceGuard(False)
    return ExecutorInstanceGuard(True, handle, kernel32.CloseHandle)
