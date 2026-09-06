"""Local, opt-in HubStudio cache inspection. Never delete whole profiles.

Only directory metadata is inspected, except SYSTEM_CONFIG.cachePath in
HubStudio's read-only settings database. Account databases are never opened.
"""
import copy
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import sqlite3
import stat
import subprocess
import sys
import threading
import time

from .hub_api import _windows_hidden_process_kwargs


PROFILE_NAME = re.compile(r'chromium_[0-9]+\Z')
KINDS = {
    'web': ('网页资源缓存', ('Default/Cache',)),
    'code': ('脚本与渲染缓存', (
        'Default/Code Cache', 'Default/GPUCache', 'Default/DawnGraphiteCache',
        'Default/DawnWebGPUCache', 'ShaderCache', 'GrShaderCache',
        'GraphiteDawnCache')),
    'client': ('客户端界面缓存', (
        'Cache', 'Code Cache', 'GPUCache', 'DawnCache')),
}
PROCESS_MARKERS = ('hubstudio', 'chrobrowser', 'firebrowser', 'sbincore')


class HubCacheError(RuntimeError):
    def __init__(self, code, message, status=409):
        self.code, self.status = code, status
        super().__init__(message)


def linked(info):
    # Windows junctions are reparse points, but need not be symbolic links.
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, 'st_file_attributes', 0) & 0x400)


def single_link(path, info):
    """Require one filesystem link, including Windows volumes reporting zero."""
    link_count = getattr(info, 'st_nlink', None)
    if link_count == 1:
        return True
    if link_count not in (None, 0) or sys.platform != 'win32':
        return False
    handle = None
    try:
        import ctypes
        from ctypes import wintypes

        class FileInformation(ctypes.Structure):
            _fields_ = [
                ('attributes', wintypes.DWORD),
                ('creation_time', wintypes.FILETIME),
                ('access_time', wintypes.FILETIME),
                ('write_time', wintypes.FILETIME),
                ('volume_serial', wintypes.DWORD),
                ('size_high', wintypes.DWORD),
                ('size_low', wintypes.DWORD),
                ('number_of_links', wintypes.DWORD),
                ('file_index_high', wintypes.DWORD),
                ('file_index_low', wintypes.DWORD),
            ]

        kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
        create_file = kernel32.CreateFileW
        create_file.argtypes = [wintypes.LPCWSTR, wintypes.DWORD,
                                wintypes.DWORD, wintypes.LPVOID,
                                wintypes.DWORD, wintypes.DWORD,
                                wintypes.HANDLE]
        create_file.restype = wintypes.HANDLE
        get_info = kernel32.GetFileInformationByHandle
        get_info.argtypes = [wintypes.HANDLE, ctypes.POINTER(FileInformation)]
        get_info.restype = wintypes.BOOL
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = [wintypes.HANDLE]
        close_handle.restype = wintypes.BOOL

        handle = create_file(str(path), 0, 0x1 | 0x2 | 0x4, None, 3,
                             0x00200000, None)
        if handle in (None, ctypes.c_void_p(-1).value):
            handle = None
            return False
        details = FileInformation()
        return bool(get_info(handle, ctypes.byref(details))
                    and details.number_of_links == 1)
    except (AttributeError, OSError, TypeError, ValueError):
        return False
    finally:
        if handle is not None:
            try:
                close_handle(handle)
            except (NameError, OSError):
                pass


def plain_directory(path):
    """Reject symlinks/junctions in every component, including the root."""
    path = Path(os.path.abspath(path))
    for part in (*reversed(path.parents), path):
        info = part.lstat()
        if linked(info) or not stat.S_ISDIR(info.st_mode):
            raise OSError('unsafe directory')
    return path


def identity(path):
    info = plain_directory(path).stat()
    return info.st_dev, info.st_ino


def process_paths():
    """Read executable names/paths only; never process arguments."""
    try:
        if sys.platform == 'win32':
            result = subprocess.run([
                'powershell.exe', '-NoProfile', '-NonInteractive', '-Command',
                "$ErrorActionPreference='Stop'; "
                "[Console]::OutputEncoding=[Text.UTF8Encoding]::new(); "
                "Get-CimInstance Win32_Process | "
                "Select-Object Name,ExecutablePath | ConvertTo-Json -Compress",
            ], capture_output=True, text=True, encoding='utf-8', timeout=12, check=True,
                **_windows_hidden_process_kwargs())
            rows = json.loads(result.stdout or '[]')
            if isinstance(rows, dict):
                rows = [rows]
            return [str(row.get('ExecutablePath') or row.get('Name') or '')
                    for row in rows]
        result = subprocess.run(
            ['/bin/ps', '-axo', 'comm='], capture_output=True, text=True,
            timeout=5, check=True)
        return result.stdout.splitlines()
    except (OSError, ValueError, subprocess.SubprocessError):
        raise HubCacheError('hub_cache_process_unknown',
                            '无法确认 HubStudio 进程状态，请稍后重试')


def require_hub_closed():
    if any(any(marker in name.lower() for marker in PROCESS_MARKERS)
           for name in process_paths()):
        raise HubCacheError('hub_cache_browser_running',
                            '请关闭所有环境、等待归档完成，并从托盘退出 HubStudio 后再清理')


def windows_installations():
    """Find non-default drives through Windows uninstall registration."""
    import winreg
    found = []
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
            try:
                with winreg.OpenKey(hive,
                                    r'SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall',
                                    0, winreg.KEY_READ | view) as parent:
                    for i in range(winreg.QueryInfoKey(parent)[0]):
                        try:
                            with winreg.OpenKey(parent, winreg.EnumKey(parent, i)) as key:
                                name = str(winreg.QueryValueEx(key, 'DisplayName')[0])
                                if 'hubstudio' not in name.lower():
                                    continue
                                try:
                                    location = winreg.QueryValueEx(key, 'InstallLocation')[0]
                                except OSError:
                                    icon = str(winreg.QueryValueEx(key, 'DisplayIcon')[0])
                                    location = str(Path(icon.split(',')[0].strip('"')).parent)
                                if location:
                                    found.append(Path(location))
                        except OSError:
                            continue
            except OSError:
                continue
    return found


def configured_cache(data_root):
    database = data_root / 'ELECTRON_DB_LOCAL_STORAGE'
    if not database.is_file() or linked(database.lstat()):
        return None
    connection = None
    try:
        plain_directory(database.parent)
        connection = sqlite3.connect(database.as_uri() + '?mode=ro', uri=True,
                                     timeout=1)
        row = connection.execute(
            'SELECT value FROM localstorage WHERE key = ? LIMIT 1',
            ('SYSTEM_CONFIG',)).fetchone()
        settings = json.loads(row[0]) if row else {}
        value = settings.get('cachePath') if isinstance(settings, dict) else None
        if isinstance(value, str) and Path(value).is_absolute():
            return Path(value)
    except (OSError, ValueError, sqlite3.Error):
        pass
    finally:
        if connection:
            connection.close()
    return None


def discover_roots(custom_path='', home=None, platform=None, environ=None,
                   installations=None):
    home = Path(home or Path.home())
    platform = platform or sys.platform
    environ = os.environ if environ is None else environ
    installations = list(installations or [])
    roots = []
    if platform == 'darwin':
        installations += [Path('/Applications/Hubstudio.app'),
                          home / 'Applications/Hubstudio.app']
        data_roots = [home / 'Library/Application Support/hubstudio-client']
        roots.append((home / 'Library/Caches/hubstudio-client/sdk/cache',
                      'profiles', True))
    elif platform == 'win32':
        if sys.platform == 'win32':
            installations += windows_installations()
        data_roots = []
        for key in ('APPDATA', 'LOCALAPPDATA'):
            if environ.get(key):
                data_roots += [Path(environ[key]) / name
                               for name in ('hubstudio-client', 'Hubstudio')]
        for key in ('ProgramFiles', 'ProgramFiles(x86)', 'LOCALAPPDATA'):
            if environ.get(key):
                base = Path(environ[key])
                installations += [base / 'Hubstudio', base / 'Programs/Hubstudio']
        try:
            for value in process_paths():
                exe = Path(value)
                if exe.is_absolute() and exe.name.lower() == 'hubstudio.exe':
                    installations.append(exe.parent)
        except HubCacheError:
            pass
        data_roots += installations
    else:
        data_roots = [home / '.config/hubstudio-client']
    for root in data_roots:
        roots.append((root, 'client', False))
        roots.append((root / 'sdk/cache', 'profiles', False))
        path = configured_cache(root)
        if path:
            roots.append((path, 'profiles', False))
    if custom_path:
        if not isinstance(custom_path, str) or len(custom_path) > 2048:
            raise HubCacheError('hub_cache_path_invalid', '请输入有效的本地文件夹路径', 400)
        custom = Path(custom_path).expanduser()
        if not custom.is_absolute():
            raise HubCacheError('hub_cache_path_invalid', '请填写完整的本地绝对路径', 400)
        try:
            plain_directory(custom)
            children = list(custom.iterdir())
        except OSError:
            raise HubCacheError('hub_cache_path_invalid', '目录不存在、不可访问或包含链接', 400)
        if (custom / 'sdk/cache').is_dir():
            roots.append((custom, 'client', False))
            roots.append((custom / 'sdk/cache', 'profiles', False))
        elif any(PROFILE_NAME.fullmatch(p.name) for p in children):
            roots.append((custom, 'profiles', False))
        else:
            raise HubCacheError('hub_cache_path_unrecognized',
                                '未识别到 HubStudio 环境目录，请复制本地设置中的缓存路径', 400)
    return roots, [str(p) for p in dict.fromkeys(installations) if p.exists()]


def inspect_cache(roots):
    groups = {key: {'id': key, 'label': label, 'bytes': 0, 'fileCount': 0,
                    'directoryCount': 0} for key, (label, _) in KINDS.items()}
    targets, locations, warnings = [], [], []
    seen_roots, seen_targets = set(), set()
    profile_count = 0
    for root, kind, cache_only in roots:
        root = Path(os.path.abspath(root))
        key = os.path.normcase(str(root))
        if key in seen_roots or not root.exists():
            continue
        seen_roots.add(key)
        try:
            anchor_id = identity(root)
            candidates = []
            if kind == 'client':
                # Do not treat an arbitrary installation directory as a profile.
                if not (root / 'ELECTRON_DB_LOCAL_STORAGE').is_file():
                    continue
                candidates = [(root / rel, 'client') for rel in KINDS['client'][1]]
            else:
                for profile in root.iterdir():
                    if not PROFILE_NAME.fullmatch(profile.name):
                        continue
                    plain_directory(profile)
                    if not (profile / 'Default').is_dir():
                        continue
                    if not cache_only and not (profile / 'Local State').is_file():
                        continue
                    profile_count += 1
                    for group in ('web', 'code'):
                        candidates.extend((profile / rel, group)
                                          for rel in KINDS[group][1])
            cleanable = 0
            for path, group in candidates:
                if not path.exists():
                    continue
                target_key = os.path.normcase(str(path))
                if target_key in seen_targets:
                    continue
                seen_targets.add(target_key)
                try:
                    target_id = identity(path)
                    size, count, errors = measure(path)
                    if errors:
                        warnings.append('部分缓存文件无法读取，显示大小为已读取部分')
                    targets.append((path, group, root, anchor_id, target_id))
                    groups[group]['bytes'] += size
                    groups[group]['fileCount'] += count
                    groups[group]['directoryCount'] += 1
                    cleanable += size
                except OSError:
                    warnings.append('已跳过不可访问或包含链接的缓存目录')
            try:
                free = shutil.disk_usage(root).free
            except OSError:
                free = None
            locations.append({'path': str(root), 'kind': kind,
                              'cleanableBytes': cleanable, 'diskFreeBytes': free})
        except OSError:
            warnings.append('部分环境目录不可访问或包含链接，未纳入清理范围')
    return {'groups': list(groups.values()), 'locations': locations,
            'cleanableBytes': sum(g['bytes'] for g in groups.values()),
            'profileDirectoryCount': profile_count,
            'warnings': list(dict.fromkeys(warnings))}, targets


def files_in(path, errors):
    """Walk only ordinary directories/files; no link, junction or hardlink."""
    pending = [path]
    while pending:
        directory = pending.pop()
        try:
            plain_directory(directory)
            with os.scandir(directory) as entries:
                for entry in entries:
                    info = entry.stat(follow_symlinks=False)
                    if linked(info):
                        errors.append(1)
                    elif stat.S_ISDIR(info.st_mode):
                        pending.append(Path(entry.path))
                    elif stat.S_ISREG(info.st_mode) and single_link(
                            Path(entry.path), info):
                        yield Path(entry.path), info
                    else:
                        errors.append(1)
        except OSError:
            errors.append(1)


def measure(path):
    size, count, errors = 0, 0, []
    for _, info in files_in(path, errors):
        size += info.st_size
        count += 1
    return size, count, len(errors)


class HubCacheManager:
    def __init__(self, tasks, discover=discover_roots, closed_check=require_hub_closed):
        self.tasks, self.discover, self.closed_check = tasks, discover, closed_check
        self.lock = threading.RLock()
        self.targets = []
        self.scanned_at = 0
        self.state = {'state': 'idle', 'running': False, 'groups': [],
                      'locations': [], 'message': '点击检测，查看本机可清理的缓存',
                      'errorCode': '', 'scanId': ''}

    def snapshot(self):
        with self.lock:
            return copy.deepcopy(self.state)

    def scan(self, custom_path=''):
        with self.lock:
            self._idle()
            self.targets = []
            self.scanned_at = 0
            self.state = {'state': 'scanning', 'running': True, 'groups': [],
                          'locations': [], 'scanId': '', 'errorCode': '',
                          'message': '正在检测缓存大小…'}
            threading.Thread(target=self._scan, args=(custom_path,), daemon=True,
                             name='xynigo-hub-cache-scan').start()
        return self.snapshot()

    def _scan(self, custom_path):
        try:
            roots, installs = self.discover(custom_path)
            report, targets = inspect_cache(roots)
            with self.lock:
                self.targets = targets
                self.scanned_at = time.monotonic()
                self.state.update(report, state='ready', running=False,
                                  installations=installs, scanId=secrets.token_hex(16),
                                  scannedAt=time.strftime('%Y-%m-%d %H:%M:%S'),
                                  message='检测完成，请选择要清理的缓存类别')
        except Exception as exc:
            self._failed(exc, '缓存检测失败，请检查目录访问权限')

    def _idle(self):
        if self.state['running']:
            raise HubCacheError('hub_cache_busy', '缓存检测或清理正在进行')

    def clear(self, scan_id, groups, confirmed=False):
        with self.lock:
            self._idle()
            if confirmed is not True:
                raise HubCacheError('hub_cache_confirmation_required', '请先确认选中的缓存类别', 400)
            if (not scan_id or scan_id != self.state.get('scanId')
                    or not self.scanned_at or time.monotonic() - self.scanned_at > 1800):
                raise HubCacheError('hub_cache_scan_expired', '检测结果已失效，请重新检测')
            if (not isinstance(groups, list) or not groups
                    or any(not isinstance(g, str) or g not in KINDS for g in groups)):
                raise HubCacheError('hub_cache_selection_invalid', '请选择有效的缓存类别', 400)
            selected = [t for t in self.targets if t[1] in groups]
            if not selected:
                raise HubCacheError('hub_cache_empty', '选中的类别没有可清理缓存')
            task_id = self.tasks.begin('hub_cache')
            try:
                self.closed_check()
            except Exception:
                self.tasks.finish(task_id)
                raise
            self.state.update(state='cleaning', running=True, errorCode='', scanId='',
                              removedBytes=0, removedFiles=0, skippedFiles=0,
                              message='正在清理选中的缓存，请保持 HubStudio 关闭')
            threading.Thread(target=self._clear, args=(selected, task_id), daemon=True,
                             name='xynigo-hub-cache-clear').start()
        return self.snapshot()

    def _clear(self, selected, task_id):
        removed, count, errors = 0, 0, []
        stopped = None
        try:
            next_check = 0
            for path, _, root, _, _ in selected:
                if time.monotonic() >= next_check:
                    self.closed_check()
                    next_check = time.monotonic() + 1
                try:
                    root = plain_directory(root)
                    path = plain_directory(path)
                    if path == root:
                        raise OSError('unsafe cache target')
                    path.relative_to(root)
                    for file, info in files_in(path, errors):
                        if time.monotonic() >= next_check:
                            self.closed_check()
                            next_check = time.monotonic() + 1
                        try:
                            plain_directory(file.parent)
                            current = file.lstat()
                            if (linked(current) or not stat.S_ISREG(current.st_mode)
                                    or not single_link(file, current)
                                    or not os.path.samestat(info, current)
                                    or current.st_size != info.st_size):
                                raise OSError('file changed')
                            file.unlink()
                            removed += current.st_size
                            count += 1
                        except OSError:
                            errors.append(1)
                    with self.lock:
                        self.state.update(removedBytes=removed, removedFiles=count,
                                          skippedFiles=len(errors))
                except OSError:
                    errors.append(1)
        except Exception as exc:
            stopped = exc
        finally:
            self.tasks.finish(task_id)
            with self.lock:
                self.targets = []
                self.scanned_at = 0
                self.state.update(state='partial' if errors or stopped else 'complete',
                                  running=False, removedBytes=removed, removedFiles=count,
                                  skippedFiles=len(errors),
                                  errorCode=getattr(stopped, 'code', '') if stopped else '',
                                  message=str(stopped) if isinstance(stopped, HubCacheError)
                                  else ('部分文件未能清理，请重新检测' if errors or stopped
                                        else '清理完成，可重新检测剩余缓存'))

    def _failed(self, exc, message):
        with self.lock:
            self.state.update(state='failed', running=False,
                              errorCode=getattr(exc, 'code', 'hub_cache_failed'),
                              message=str(exc) if isinstance(exc, HubCacheError) else message)
