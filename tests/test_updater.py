# -*- coding: utf-8 -*-
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import zipfile

from purchase_tool.updater import (
    GitHubUpdateClient, MACOS_MANAGED_PATHS, MANAGED_PATHS,
    NetworkTransport, ReleaseAsset, ReleaseInfo, UpdateError,
    StandardInstallerUpdateClient, UpdateCoordinator,
    check_for_updates_at_startup,
    current_platform_key, is_newer, is_release_newer,
    safe_extract_zip, select_platform_manifest, sha256_file,
)


def release(version='0.6.0', manifest=None, assets=(), runtime_id=''):
    return ReleaseInfo(
        version=version,
        tag='v' + version,
        notes_zh=('更新说明一', '更新说明二'),
        manifest=manifest or {},
        assets=tuple(assets),
        runtime_id=runtime_id)


class FakeClient(object):
    def __init__(self, latest=None, error=None):
        self.latest = latest or release()
        self.error = error
        self.prepared = False
        self.launched = False

    def get_latest_release(self):
        if self.error:
            raise self.error
        return self.latest

    def prepare_update(
            self, _release, output=print, progress=None, stage=None):
        self.prepared = True
        if progress:
            progress(5, 10)
        output('下载进度：100%')
        if progress:
            progress(10, 10)
        if stage:
            stage('verifying', '下载完成，正在校验 SHA-256…')
        return object()

    def launch_installer(self, _prepared, _install_dir, _current_version):
        self.launched = True


class InterruptedResponse(object):
    headers = {'Content-Length': '10'}

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, _size):
        raise OSError('connection reset')


class InterruptedOpener(object):
    def open(self, _request, timeout=0):
        return InterruptedResponse()


class WritingTransport(object):
    def __init__(self, data):
        self.data = data

    def download(self, _url, target, progress=None):
        Path(target).write_bytes(self.data)
        if progress:
            progress(len(self.data), len(self.data))


class FakeStandardAuth(object):
    def __init__(self, data, platform_key='windows-x86_64'):
        self.data = data
        self.platform_key = platform_key
        self.digest = __import__('hashlib').sha256(data).hexdigest()
        self.downloads = []

    def local_executor_release_catalog(self):
        return {
            'schemaVersion': 1,
            'version': '0.12.7',
            'channel': 'test',
            'notesZh': ['标准安装包在线升级'],
            'platforms': {
                self.platform_key: {
                    'runtimeId': '0.12.7-newbuild001',
                    'installMode': (
                        'standard_system_application'
                        if self.platform_key.startswith('macos-')
                        else 'standard_per_user'),
                    'assetName': (
                        'Xynigo_Setup.pkg'
                        if self.platform_key.startswith('macos-')
                        else 'Xynigo_Setup.exe'),
                    'downloadUrl': (
                        '/v1/local-executor/releases/'
                        + self.platform_key + '/primary/download'),
                    'sha256': self.digest,
                    'size': len(self.data),
                    'internalUnsignedTest': True,
                },
            },
        }

    def download_local_executor_release(
            self, path, target, *, expected_size, expected_hash,
            progress=None):
        self.downloads.append((path, expected_size, expected_hash))
        Path(target).write_bytes(self.data)
        if progress:
            progress(len(self.data), len(self.data))
        return Path(target)


class UpdaterTests(unittest.TestCase):
    def test_platform_detection_supports_windows_and_apple_silicon(self):
        self.assertEqual(
            current_platform_key('win32', 'AMD64'), 'windows-x86_64')
        self.assertEqual(
            current_platform_key('darwin', 'arm64'), 'macos-arm64')
        with self.assertRaises(UpdateError):
            current_platform_key('darwin', 'x86_64')

    def test_manifest_selects_current_platform_without_breaking_legacy_windows(self):
        manifest = {
            'version': '0.5.1',
            'assetName': 'windows.zip',
            'sha256': '1' * 64,
            'size': 10,
            'platforms': {
                'windows-x86_64': {
                    'assetName': 'windows.zip',
                    'sha256': '1' * 64,
                    'size': 10,
                },
                'macos-arm64': {
                    'assetName': 'mac-arm64.zip',
                    'sha256': '2' * 64,
                    'size': 20,
                },
            },
        }
        selected = select_platform_manifest(manifest, 'macos-arm64')
        self.assertEqual(selected['assetName'], 'mac-arm64.zip')
        legacy = select_platform_manifest(
            {'assetName': 'windows.zip'}, 'windows-x86_64')
        self.assertEqual(legacy['assetName'], 'windows.zip')
        with self.assertRaisesRegex(UpdateError, 'macOS'):
            select_platform_manifest(
                {'assetName': 'windows.zip'}, 'macos-arm64')

    def test_semantic_version_comparison(self):
        self.assertTrue(is_newer('0.5.1', '0.5.0'))
        self.assertFalse(is_newer('v0.5.0', '0.5.0'))
        same_build = release(
            '0.12.7', runtime_id='0.12.7-build001',
            manifest={'runtimeId': '0.12.7-build001'})
        new_build = release(
            '0.12.7', runtime_id='0.12.7-build002',
            manifest={'runtimeId': '0.12.7-build002'})
        self.assertFalse(is_release_newer(
            same_build, '0.12.7', '0.12.7-build001'))
        self.assertTrue(is_release_newer(
            new_build, '0.12.7', '0.12.7-build001'))

    def test_no_new_version_continues_startup(self):
        output = []
        client = FakeClient(latest=release('0.5.0'))
        launched = check_for_updates_at_startup(
            '/install', '0.5.0', client=client,
            input_fn=lambda _prompt: 'Y', output=output.append, environ={})
        self.assertFalse(launched)
        self.assertFalse(client.prepared)
        self.assertTrue(any('当前已是最新稳定版' in line for line in output))

    def test_new_version_can_be_skipped(self):
        output = []
        client = FakeClient()
        launched = check_for_updates_at_startup(
            '/install', '0.5.0', client=client,
            input_fn=lambda _prompt: 'N', output=output.append, environ={})
        self.assertFalse(launched)
        self.assertFalse(client.prepared)
        self.assertTrue(any('中文更新介绍' in line for line in output))

    def test_verified_update_launches_helper(self):
        client = FakeClient()
        launched = check_for_updates_at_startup(
            '/install', '0.5.0', client=client,
            input_fn=lambda _prompt: 'Y', output=lambda _line: None,
            environ={})
        self.assertTrue(launched)
        self.assertTrue(client.prepared)
        self.assertTrue(client.launched)

    def test_network_or_github_failure_does_not_block_startup(self):
        output = []
        client = FakeClient(error=UpdateError('GitHub unavailable'))
        launched = check_for_updates_at_startup(
            '/install', '0.5.0', client=client,
            input_fn=lambda _prompt: 'Y', output=output.append, environ={})
        self.assertFalse(launched)
        self.assertTrue(any('继续正常启动' in line for line in output))

    def test_post_update_restart_skips_exactly_one_check(self):
        client = FakeClient(error=AssertionError('must not call network'))
        launched = check_for_updates_at_startup(
            '/install', '0.5.0', client=client,
            output=lambda _line: None,
            environ={'XYNIGO_SKIP_UPDATE_ONCE': '1'})
        self.assertFalse(launched)

    def test_post_update_marker_is_consumed_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / 'skip-update-once'
            marker.write_text('1', encoding='ascii')
            client = FakeClient(error=AssertionError('must not call network'))
            launched = check_for_updates_at_startup(
                '/install', '0.5.0', client=client,
                output=lambda _line: None, environ={},
                skip_marker_path=marker)
            self.assertFalse(launched)
            self.assertFalse(marker.exists())

    def test_web_coordinator_reports_available_release(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = UpdateCoordinator(
                tmp, '0.5.0', client=FakeClient(), environ={},
                skip_marker_path=Path(tmp) / 'no-marker')
            status = manager.check_now()
        self.assertEqual(status['state'], 'available')
        self.assertEqual(status['latestVersion'], '0.6.0')
        self.assertEqual(status['notes'], ['更新说明一', '更新说明二'])

    def test_web_coordinator_console_prompt_can_skip(self):
        focus_calls = []
        client = FakeClient()
        with tempfile.TemporaryDirectory() as tmp:
            manager = UpdateCoordinator(
                tmp, '0.5.0', client=client, input_fn=lambda _prompt: 'N',
                output=lambda _line: None,
                focus_fn=lambda: focus_calls.append(True) or True,
                environ={}, skip_marker_path=Path(tmp) / 'no-marker')
            manager.check_now()
            launched = manager.prompt_now()
            status = manager.snapshot()
        self.assertFalse(launched)
        self.assertEqual(focus_calls, [True])
        self.assertEqual(status['state'], 'available')
        self.assertEqual(status['decision'], 'skipped')
        self.assertFalse(client.prepared)

    def test_web_coordinator_console_prompt_can_install(self):
        exit_codes = []
        client = FakeClient()
        with tempfile.TemporaryDirectory() as tmp:
            manager = UpdateCoordinator(
                tmp, '0.5.0', client=client, input_fn=lambda _prompt: 'Y',
                output=lambda _line: None, focus_fn=lambda: True,
                exit_fn=exit_codes.append, environ={},
                skip_marker_path=Path(tmp) / 'no-marker')
            manager.check_now()
            launched = manager.prompt_now()
            status = manager.snapshot()
        self.assertTrue(launched)
        self.assertTrue(client.prepared)
        self.assertTrue(client.launched)
        self.assertEqual(exit_codes, [42])
        self.assertEqual(status['state'], 'restarting')
        self.assertEqual(status['stage'], 'restarting')
        self.assertEqual(status['downloadPercent'], 100)
        self.assertEqual(status['downloadReceivedBytes'], 10)
        self.assertEqual(status['downloadTotalBytes'], 10)

    def test_download_progress_snapshot_exposes_size_speed_and_eta(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = UpdateCoordinator(
                tmp, '0.5.0', client=FakeClient(), environ={},
                skip_marker_path=Path(tmp) / 'no-marker')
            with manager.lock:
                manager.state = 'downloading'
                manager._reset_download_progress_locked()
                manager.download_started_at -= 2
            manager._download_progress(5 * 1024 * 1024, 10 * 1024 * 1024)
            status = manager.snapshot()
        self.assertEqual(status['state'], 'downloading')
        self.assertEqual(status['stage'], 'downloading')
        self.assertEqual(status['downloadPercent'], 50)
        self.assertEqual(status['downloadReceivedBytes'], 5 * 1024 * 1024)
        self.assertEqual(status['downloadTotalBytes'], 10 * 1024 * 1024)
        self.assertGreater(status['downloadSpeedBytesPerSecond'], 0)
        self.assertGreaterEqual(status['downloadEtaSeconds'], 1)
        self.assertIn('50%', status['message'])
        self.assertIn('5.0 MB/10.0 MB', status['message'])

    def test_standard_coordinator_confirms_in_web_and_updates_same_version(self):
        exit_codes = []
        client = FakeClient(latest=release(
            '0.12.7',
            manifest={'runtimeId': '0.12.7-build002'},
            runtime_id='0.12.7-build002'))
        with tempfile.TemporaryDirectory() as tmp:
            manager = UpdateCoordinator(
                tmp,
                '0.12.7',
                client=client,
                current_runtime_id='0.12.7-build001',
                input_fn=lambda _prompt: self.fail('standard updater must not prompt'),
                focus_fn=lambda: self.fail('standard updater has no console'),
                output=lambda _line: None,
                exit_fn=exit_codes.append,
                environ={'XYNIGO_INSTALL_MODE': 'standard'},
                skip_marker_path=Path(tmp) / 'no-marker',
                standard_install_delay=0,
            )
            checked = manager.check_now()
            installed = manager.prompt_now()
            status = manager.snapshot()
        self.assertEqual(checked['state'], 'available')
        self.assertEqual(checked['confirmationMode'], 'direct')
        self.assertTrue(installed)
        self.assertTrue(client.prepared)
        self.assertTrue(client.launched)
        self.assertEqual(exit_codes, [42])
        self.assertEqual(status['state'], 'restarting')

    def test_two_standard_clients_can_download_updates_concurrently(self):
        allow_downloads = threading.Event()
        downloads_started = [threading.Event(), threading.Event()]

        class BlockingClient(FakeClient):
            def __init__(self, index):
                super(BlockingClient, self).__init__()
                self.index = index

            def prepare_update(
                    self, _release, output=print, progress=None, stage=None):
                self.prepared = True
                downloads_started[self.index].set()
                if not allow_downloads.wait(2):
                    raise UpdateError('并发下载测试等待超时')
                if progress:
                    progress(10, 10)
                if stage:
                    stage('verifying', '下载完成，正在校验 SHA-256…')
                return object()

        with tempfile.TemporaryDirectory() as tmp:
            managers = []
            exit_codes = [[], []]
            for index in range(2):
                manager = UpdateCoordinator(
                    Path(tmp) / str(index),
                    '0.5.0',
                    client=BlockingClient(index),
                    output=lambda _line: None,
                    exit_fn=exit_codes[index].append,
                    environ={'XYNIGO_INSTALL_MODE': 'standard'},
                    skip_marker_path=Path(tmp) / ('no-marker-%d' % index),
                    standard_install_delay=0,
                )
                manager.check_now()
                managers.append(manager)

            started_at = time.monotonic()
            self.assertTrue(managers[0].prompt_async())
            self.assertTrue(managers[1].prompt_async())
            self.assertLess(time.monotonic() - started_at, 0.25)
            self.assertTrue(downloads_started[0].wait(1))
            self.assertTrue(downloads_started[1].wait(1))
            self.assertEqual(
                [manager.snapshot()['state'] for manager in managers],
                ['downloading', 'downloading'],
            )

            allow_downloads.set()
            deadline = time.monotonic() + 2
            while (time.monotonic() < deadline
                   and any(manager.snapshot()['state'] != 'restarting'
                           for manager in managers)):
                time.sleep(0.01)

        self.assertEqual(
            [manager.snapshot()['state'] for manager in managers],
            ['restarting', 'restarting'],
        )
        self.assertEqual(exit_codes, [[42], [42]])

    def test_standard_client_downloads_authenticated_installer_and_checks_hash(self):
        data = b'x' * 1_100_000
        auth = FakeStandardAuth(data)
        client = StandardInstallerUpdateClient(
            auth, platform_key='windows-x86_64')
        info = client.get_latest_release()
        self.assertEqual(info.runtime_id, '0.12.7-newbuild001')
        prepared = client.prepare_update(info, output=lambda _line: None)
        try:
            self.assertEqual(prepared.package_root.read_bytes(), data)
            self.assertEqual(len(auth.downloads), 1)
        finally:
            import shutil
            shutil.rmtree(str(prepared.work_dir), ignore_errors=True)

    def test_standard_client_downloads_verified_macos_pkg(self):
        data = b'm' * 1_100_000
        auth = FakeStandardAuth(data, platform_key='macos-arm64')
        client = StandardInstallerUpdateClient(
            auth, platform_key='macos-arm64')
        info = client.get_latest_release()
        prepared = client.prepare_update(info, output=lambda _line: None)
        try:
            self.assertEqual(prepared.package_root.suffix, '.pkg')
            self.assertEqual(prepared.package_root.read_bytes(), data)
            self.assertEqual(client.install_flow, 'macos_system_installer')
            self.assertEqual(len(auth.downloads), 1)
        finally:
            import shutil
            shutil.rmtree(str(prepared.work_dir), ignore_errors=True)

    def test_macos_standard_client_opens_verified_pkg_with_system_installer(self):
        data = b'm' * 1_100_000
        auth = FakeStandardAuth(data, platform_key='macos-arm64')
        client = StandardInstallerUpdateClient(
            auth, platform_key='macos-arm64')
        prepared = client.prepare_update(
            client.get_latest_release(), output=lambda _line: None)
        try:
            with patch('purchase_tool.updater.sys.platform', 'darwin'), patch(
                    'purchase_tool.updater.subprocess.Popen') as popen:
                client.launch_installer(prepared, '/Applications', '0.12.6')
            command = popen.call_args.args[0]
            self.assertEqual(command[0], '/usr/bin/open')
            self.assertEqual(Path(command[1]), prepared.package_root.resolve())
            self.assertTrue(popen.call_args.kwargs['start_new_session'])
        finally:
            import shutil
            shutil.rmtree(str(prepared.work_dir), ignore_errors=True)

    def test_standard_catalog_rejects_unsigned_package_outside_test_channel(self):
        data = b'x' * 1_100_000
        auth = FakeStandardAuth(data)
        catalog = auth.local_executor_release_catalog()
        catalog['channel'] = 'stable'
        auth.local_executor_release_catalog = lambda: catalog
        client = StandardInstallerUpdateClient(
            auth, platform_key='windows-x86_64')
        with self.assertRaisesRegex(UpdateError, '签名信任门槛'):
            client.get_latest_release()

    def test_source_mode_keeps_green_package_update_disabled(self):
        manager = UpdateCoordinator(None, '0.6.0')
        self.assertEqual(manager.snapshot()['state'], 'disabled')
        self.assertFalse(manager.check_async())
        self.assertFalse(manager.prompt_async())

    def test_interrupted_download_removes_partial_file(self):
        transport = NetworkTransport()
        transport.opener = InterruptedOpener()
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / 'package.zip'
            with self.assertRaises(UpdateError):
                transport.download('https://example.test/package.zip', target)
            self.assertFalse(target.exists())
            self.assertFalse((Path(tmp) / 'package.zip.part').exists())

    def test_checksum_failure_is_rejected_before_extract(self):
        data = b'not-the-expected-package'
        asset = ReleaseAsset('package.zip', 'https://example.test/package.zip',
                             len(data))
        info = release(manifest={
            'assetName': asset.name,
            'sha256': '0' * 64,
            'size': len(data),
        }, assets=(asset,))
        client = GitHubUpdateClient(transport=WritingTransport(data))
        with self.assertRaisesRegex(UpdateError, 'SHA-256'):
            client.prepare_update(info, output=lambda _line: None)

    def test_safe_extract_rejects_path_traversal(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / 'bad.zip'
            with zipfile.ZipFile(archive, 'w') as handle:
                handle.writestr('../outside.txt', 'no')
            with self.assertRaises(UpdateError):
                safe_extract_zip(archive, Path(tmp) / 'out')

    @unittest.skipIf(os.name == 'nt',
                     'Windows does not expose POSIX executable bits')
    def test_safe_extract_restores_executable_permissions(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / 'executable.zip'
            member = zipfile.ZipInfo('Xynigo-Sourcing/update-helper.sh')
            member.create_system = 3
            member.external_attr = (0o100755 << 16)
            with zipfile.ZipFile(archive, 'w') as handle:
                handle.writestr(member, '#!/bin/bash\n')
            destination = Path(tmp) / 'out'
            safe_extract_zip(archive, destination)
            extracted = destination / member.filename
            self.assertTrue(extracted.stat().st_mode & 0o100)

    def test_prepare_accepts_complete_verified_package(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / 'package.zip'
            with zipfile.ZipFile(archive, 'w') as handle:
                handle.writestr(
                    'Xynigo-Sourcing/VERSION.json',
                    json.dumps({'version': '0.6.0'}))
                for name in MANAGED_PATHS:
                    if name == 'VERSION.json':
                        continue
                    if '.' in Path(name).name:
                        handle.writestr('Xynigo-Sourcing/' + name, 'content')
                    else:
                        handle.writestr('Xynigo-Sourcing/' + name + '/.keep', '')
            data = archive.read_bytes()
            asset = ReleaseAsset('package.zip', 'https://example.test/package.zip',
                                 len(data))
            info = release(manifest={
                'assetName': asset.name,
                'sha256': sha256_file(archive),
                'size': len(data),
            }, assets=(asset,))
            client = GitHubUpdateClient(transport=WritingTransport(data))
            prepared = client.prepare_update(info, output=lambda _line: None)
            try:
                self.assertEqual(prepared.release.version, '0.6.0')
                self.assertTrue((prepared.package_root / 'run.py').is_file())
            finally:
                import shutil
                shutil.rmtree(str(prepared.work_dir), ignore_errors=True)

    def test_prepare_accepts_complete_macos_package(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / 'mac-package.zip'
            with zipfile.ZipFile(archive, 'w') as handle:
                handle.writestr(
                    'Xynigo-Sourcing/VERSION.json',
                    json.dumps({'version': '0.6.0'}))
                for name in MACOS_MANAGED_PATHS:
                    if name == 'VERSION.json':
                        continue
                    if '.' in Path(name).name:
                        handle.writestr('Xynigo-Sourcing/' + name, 'content')
                    else:
                        handle.writestr(
                            'Xynigo-Sourcing/' + name + '/.keep', '')
            data = archive.read_bytes()
            asset = ReleaseAsset(
                archive.name, 'https://example.test/mac-package.zip',
                len(data))
            info = release(manifest={
                'assetName': asset.name,
                'sha256': sha256_file(archive),
                'size': len(data),
            }, assets=(asset,))
            info = ReleaseInfo(
                version=info.version, tag=info.tag,
                notes_zh=info.notes_zh, manifest=info.manifest,
                assets=info.assets, platform_key='macos-arm64',
                managed_paths=MACOS_MANAGED_PATHS)
            client = GitHubUpdateClient(
                transport=WritingTransport(data),
                platform_key='macos-arm64')
            prepared = client.prepare_update(info, output=lambda _line: None)
            try:
                self.assertTrue(
                    (prepared.package_root / 'update-helper.sh').is_file())
                if os.name != 'nt':
                    self.assertTrue(
                        prepared.helper_path.stat().st_mode & 0o100)
            finally:
                import shutil
                shutil.rmtree(str(prepared.work_dir), ignore_errors=True)


if __name__ == '__main__':
    unittest.main()
