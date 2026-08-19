# -*- coding: utf-8 -*-
"""Cross-platform green-package updater backed by public GitHub Releases.

The updater intentionally uses urllib's default proxy discovery. On Windows
that means system proxy settings are respected (including Clash Verge), while
a TUN adapter remains transparent to the process. macOS uses its configured
system networking in the same way.
"""
from dataclasses import dataclass
import hashlib
import json
import os
import platform
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
import zipfile


LATEST_RELEASE_API = (
    'https://api.github.com/repos/wrangler1024/'
    'xynigo-sourcing/releases/latest')
USER_AGENT = 'Xynigo-Sourcing-Updater'
SKIP_ONCE_ENV = 'XYNIGO_SKIP_UPDATE_ONCE'
SKIP_ONCE_FILE = 'skip-update-once'
WINDOWS_MANAGED_PATHS = (
    'app', 'deps', 'python-embed', 'run.py', '启动.bat',
    'update-helper.ps1', 'VERSION.json', '使用说明.txt',
)
MACOS_MANAGED_PATHS = (
    'runtime', '启动-Mac.command', 'update-helper.sh',
    'VERSION.json', '使用说明.txt',
)
# Kept as a public compatibility alias for v0.5.0 callers/tests.
MANAGED_PATHS = WINDOWS_MANAGED_PATHS


class UpdateError(Exception):
    pass


@dataclass(frozen=True)
class ReleaseAsset:
    name: str
    url: str
    size: int = 0


@dataclass(frozen=True)
class ReleaseInfo:
    version: str
    tag: str
    notes_zh: tuple
    manifest: dict
    assets: tuple
    platform_key: str = 'windows-x86_64'
    managed_paths: tuple = WINDOWS_MANAGED_PATHS


@dataclass(frozen=True)
class PreparedUpdate:
    release: ReleaseInfo
    work_dir: Path
    package_root: Path
    helper_path: Path


def normalize_version(value):
    text = str(value or '').strip()
    if text.lower().startswith('v'):
        text = text[1:]
    parts = text.split('.')
    if len(parts) != 3 or any(not item.isdigit() for item in parts):
        raise UpdateError('版本号格式无效：%s' % value)
    return '.'.join(str(int(item)) for item in parts)


def version_key(value):
    return tuple(int(item) for item in normalize_version(value).split('.'))


def is_newer(latest, current):
    return version_key(latest) > version_key(current)


def current_platform_key(system=None, machine=None):
    system = (system or sys.platform).lower()
    machine = (machine or platform.machine()).lower()
    if system.startswith('win'):
        return 'windows-x86_64'
    if system == 'darwin':
        if machine in ('arm64', 'aarch64'):
            return 'macos-arm64'
        if machine in ('x86_64', 'amd64'):
            return 'macos-x86_64'
    raise UpdateError('当前系统暂无可用的绿色包更新')


def managed_paths_for_platform(platform_key):
    if platform_key.startswith('windows-'):
        return WINDOWS_MANAGED_PATHS
    if platform_key.startswith('macos-'):
        return MACOS_MANAGED_PATHS
    raise UpdateError('更新清单包含未知平台：%s' % platform_key)


def select_platform_manifest(manifest, platform_key):
    selected = dict(manifest)
    platforms = manifest.get('platforms')
    if platforms is None:
        if not platform_key.startswith('windows-'):
            raise UpdateError('Release 尚未提供 macOS 绿色包')
        return selected
    if not isinstance(platforms, dict):
        raise UpdateError('更新清单 platforms 格式无效')
    aliases = [platform_key]
    if platform_key.startswith('windows-'):
        aliases.append('windows')
    elif platform_key.startswith('macos-'):
        aliases.append('macos')
    package = next((platforms.get(key) for key in aliases
                    if isinstance(platforms.get(key), dict)), None)
    if package is None:
        raise UpdateError('Release 缺少 %s 绿色包' % platform_key)
    selected.update(package)
    return selected


def default_state_dir(environ=None, platform_key=None):
    environ = os.environ if environ is None else environ
    if platform_key is None:
        try:
            platform_key = current_platform_key()
        except UpdateError:
            platform_key = ''
    if platform_key.startswith('windows-'):
        base = environ.get('LOCALAPPDATA')
        if base:
            return Path(base) / 'XynigoSourcing'
        return Path.home() / 'AppData' / 'Local' / 'XynigoSourcing'
    if platform_key.startswith('macos-'):
        return Path.home() / 'Library' / 'Application Support' / 'XynigoSourcing'
    base = environ.get('XDG_STATE_HOME')
    return Path(base) / 'xynigo-sourcing' if base else (
        Path.home() / '.local' / 'state' / 'xynigo-sourcing')


def consume_skip_once(environ=None, marker_path=None):
    environ = os.environ if environ is None else environ
    if environ.get(SKIP_ONCE_ENV) == '1':
        return True
    if marker_path is None:
        try:
            marker_path = default_state_dir(environ) / SKIP_ONCE_FILE
        except UpdateError:
            return False
    marker_path = Path(marker_path)
    if not marker_path.is_file():
        return False
    try:
        marker_path.unlink()
    except OSError:
        return False
    return True


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


class NetworkTransport(object):
    """HTTPS transport that inherits the operating-system proxy settings."""

    def __init__(self, timeout=6):
        self.timeout = timeout
        self.opener = urllib.request.build_opener()

    def _request(self, url, accept):
        return urllib.request.Request(
            url,
            headers={'Accept': accept, 'User-Agent': USER_AGENT})

    def get_json(self, url):
        request = self._request(url, 'application/vnd.github+json')
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                return json.loads(response.read().decode('utf-8'))
        except (OSError, ValueError, urllib.error.URLError) as exc:
            raise UpdateError('网络请求失败：%s' % exc) from exc

    def download(self, url, target, progress=None):
        target = Path(target)
        partial = target.with_name(target.name + '.part')
        request = self._request(url, 'application/octet-stream')
        try:
            with self.opener.open(request, timeout=30) as response:
                total = int(response.headers.get('Content-Length') or 0)
                received = 0
                with open(partial, 'wb') as handle:
                    while True:
                        chunk = response.read(1024 * 1024)
                        if not chunk:
                            break
                        handle.write(chunk)
                        received += len(chunk)
                        if progress:
                            progress(received, total)
            os.replace(str(partial), str(target))
        except Exception as exc:
            try:
                partial.unlink()
            except OSError:
                pass
            if isinstance(exc, UpdateError):
                raise
            raise UpdateError('下载中断：%s' % exc) from exc


def safe_extract_zip(archive_path, destination):
    destination = Path(destination).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    try:
        with zipfile.ZipFile(str(archive_path)) as archive:
            for member in archive.infolist():
                name = member.filename.replace('\\', '/')
                if not name or name.startswith('/'):
                    raise UpdateError('更新包包含非法路径')
                target = (destination / name).resolve()
                try:
                    target.relative_to(destination)
                except ValueError as exc:
                    raise UpdateError('更新包包含越界路径') from exc
                mode = (member.external_attr >> 16) & 0o170000
                if mode == stat.S_IFLNK:
                    raise UpdateError('更新包不得包含符号链接')
                if member.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(member) as source, open(target, 'wb') as out:
                    shutil.copyfileobj(source, out)
                permissions = (member.external_attr >> 16) & 0o777
                if permissions:
                    target.chmod(permissions)
    except (OSError, zipfile.BadZipFile) as exc:
        raise UpdateError('更新包解压失败：%s' % exc) from exc


def locate_package_root(extract_dir, expected_version, managed_paths=None):
    matches = list(Path(extract_dir).glob('*/VERSION.json'))
    if len(matches) != 1:
        raise UpdateError('更新包目录结构无效')
    root = matches[0].parent
    try:
        info = json.loads(matches[0].read_text(encoding='utf-8'))
    except (OSError, ValueError) as exc:
        raise UpdateError('VERSION.json 无效') from exc
    if normalize_version(info.get('version')) != normalize_version(expected_version):
        raise UpdateError('更新包版本与 Release 不一致')
    managed_paths = tuple(managed_paths or MANAGED_PATHS)
    missing = [name for name in managed_paths if not (root / name).exists()]
    if missing:
        raise UpdateError('更新包缺少程序文件：%s' % ', '.join(missing))
    return root


class GitHubUpdateClient(object):
    def __init__(self, transport=None, latest_api=LATEST_RELEASE_API,
                 platform_key=None):
        self.transport = transport or NetworkTransport()
        self.latest_api = latest_api
        self.platform_key = platform_key

    @staticmethod
    def _assets(payload):
        return tuple(ReleaseAsset(
            name=str(item.get('name') or ''),
            url=str(item.get('browser_download_url') or ''),
            size=int(item.get('size') or 0))
            for item in (payload.get('assets') or []))

    def get_latest_release(self):
        platform_key = self.platform_key or current_platform_key()
        payload = self.transport.get_json(self.latest_api)
        if payload.get('draft') or payload.get('prerelease'):
            raise UpdateError('GitHub latest 返回的不是稳定版')
        tag = str(payload.get('tag_name') or '')
        version = normalize_version(tag)
        assets = self._assets(payload)
        manifest_asset = next(
            (item for item in assets if item.name.endswith('_update.json')),
            None)
        if manifest_asset is None or not manifest_asset.url:
            raise UpdateError('Release 缺少更新清单')
        manifest = self.transport.get_json(manifest_asset.url)
        if normalize_version(manifest.get('version')) != version:
            raise UpdateError('更新清单版本与 Release 不一致')
        selected_manifest = select_platform_manifest(
            manifest, platform_key)
        notes = manifest.get('notesZh') or []
        if not isinstance(notes, list):
            notes = []
        return ReleaseInfo(
            version=version,
            tag=tag,
            notes_zh=tuple(str(item) for item in notes[:8]),
            manifest=selected_manifest,
            assets=assets,
            platform_key=platform_key,
            managed_paths=managed_paths_for_platform(platform_key))

    def prepare_update(self, release, output=print):
        asset_name = str(release.manifest.get('assetName') or '')
        expected_hash = str(release.manifest.get('sha256') or '').lower()
        expected_size = int(release.manifest.get('size') or 0)
        if (len(expected_hash) != 64
                or any(ch not in '0123456789abcdef' for ch in expected_hash)):
            raise UpdateError('更新清单 SHA-256 无效')
        asset = next((item for item in release.assets
                      if item.name == asset_name), None)
        if asset is None or not asset.url:
            raise UpdateError('Release 缺少当前平台更新包')
        if expected_size and asset.size and expected_size != asset.size:
            raise UpdateError('Release 文件大小与更新清单不一致')

        work_dir = Path(tempfile.mkdtemp(prefix='xynigo-update-'))
        archive = work_dir / asset.name
        last_percent = [-1]

        def progress(received, total):
            if not total:
                return
            percent = int(received * 100 / total)
            if percent == 100 or percent >= last_percent[0] + 10:
                last_percent[0] = percent
                output('下载进度：%d%%' % percent)

        try:
            self.transport.download(asset.url, archive, progress=progress)
            if expected_size and archive.stat().st_size != expected_size:
                raise UpdateError('下载文件大小不一致')
            actual_hash = sha256_file(archive)
            if actual_hash.lower() != expected_hash:
                raise UpdateError('SHA-256 校验失败，已拒绝更新')
            extract_dir = work_dir / 'extracted'
            safe_extract_zip(archive, extract_dir)
            package_root = locate_package_root(
                extract_dir, release.version, release.managed_paths)
            helper_name = ('update-helper.ps1'
                           if release.platform_key.startswith('windows-')
                           else 'update-helper.sh')
            helper = work_dir / helper_name
            shutil.copy2(str(package_root / helper_name), str(helper))
            if helper_name.endswith('.sh'):
                helper.chmod(helper.stat().st_mode | stat.S_IXUSR)
            return PreparedUpdate(
                release=release,
                work_dir=work_dir,
                package_root=package_root,
                helper_path=helper)
        except Exception:
            shutil.rmtree(str(work_dir), ignore_errors=True)
            raise

    @staticmethod
    def launch_installer(prepared, install_dir, current_version):
        install_dir = Path(install_dir).resolve()
        state_dir = default_state_dir(platform_key=prepared.release.platform_key)
        backup_root = state_dir / 'backups'
        backup_dir = backup_root / (
            'v%s-to-v%s' % (normalize_version(current_version),
                            normalize_version(prepared.release.version)))
        if prepared.release.platform_key.startswith('windows-'):
            if os.name != 'nt':
                raise UpdateError('Windows 更新包只能在 Windows 上安装')
            command = [
                'powershell.exe', '-NoLogo', '-NoProfile',
                '-ExecutionPolicy', 'Bypass', '-File',
                str(prepared.helper_path),
                '-InstallDir', str(install_dir),
                '-StageDir', str(prepared.package_root),
                '-BackupDir', str(backup_dir),
                '-ParentPid', str(os.getpid()),
                '-WorkDir', str(prepared.work_dir),
                '-StateDir', str(state_dir),
            ]
            popen_kwargs = {
                'creationflags': getattr(subprocess, 'CREATE_NEW_CONSOLE', 0),
            }
        elif prepared.release.platform_key.startswith('macos-'):
            if sys.platform != 'darwin':
                raise UpdateError('macOS 更新包只能在 macOS 上安装')
            command = [
                '/bin/bash', str(prepared.helper_path),
                '--install-dir', str(install_dir),
                '--stage-dir', str(prepared.package_root),
                '--backup-dir', str(backup_dir),
                '--parent-pid', str(os.getpid()),
                '--work-dir', str(prepared.work_dir),
                '--state-dir', str(state_dir),
            ]
            popen_kwargs = {'start_new_session': True}
        else:
            raise UpdateError('当前平台无法启动更新替换程序')
        try:
            subprocess.Popen(command, cwd=str(prepared.work_dir),
                             close_fds=True, **popen_kwargs)
        except OSError as exc:
            raise UpdateError('无法启动更新替换程序：%s' % exc) from exc


def check_for_updates_at_startup(install_dir, current_version,
                                 client=None, input_fn=input,
                                 output=print, environ=None,
                                 skip_marker_path=None):
    """Return True only when a verified update helper has been launched."""
    environ = os.environ if environ is None else environ
    if consume_skip_once(environ, skip_marker_path):
        output('本次为更新后自动重启，跳过一次更新检查。')
        return False
    client = client or GitHubUpdateClient()
    output('')
    output('========== Xynigo Sourcing 在线更新 =========')
    try:
        release = client.get_latest_release()
        output('当前版本：v%s' % normalize_version(current_version))
        output('最新版本：v%s' % release.version)
        if not is_newer(release.version, current_version):
            output('当前已是最新稳定版，继续启动。')
            return False
        output('中文更新介绍：')
        if release.notes_zh:
            for note in release.notes_zh:
                output('  - %s' % note)
        else:
            output('  - 请查看 GitHub Release 更新说明。')
        try:
            answer = str(input_fn('发现新版本，输入 Y 更新，输入 N 跳过：')
                         or '').strip().upper()
        except (EOFError, KeyboardInterrupt):
            answer = 'N'
        if answer != 'Y':
            output('已跳过本次更新，继续启动当前版本。')
            return False
        output('正在下载并校验更新包，请勿关闭窗口……')
        prepared = client.prepare_update(release, output=output)
        client.launch_installer(prepared, install_dir, current_version)
        output('更新包校验通过，正在切换程序并自动重启。')
        return True
    except Exception as exc:
        output('更新检查或安装失败：%s' % exc)
        output('不会影响当前版本，继续正常启动。')
        return False
    finally:
        output('==============================================')
