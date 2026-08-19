# -*- coding: utf-8 -*-
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
import zipfile

from purchase_tool.updater import (
    GitHubUpdateClient, MANAGED_PATHS, NetworkTransport, ReleaseAsset,
    ReleaseInfo, UpdateError, check_for_updates_at_startup, is_newer,
    safe_extract_zip, sha256_file,
)


def release(version='0.6.0', manifest=None, assets=()):
    return ReleaseInfo(
        version=version,
        tag='v' + version,
        notes_zh=('更新说明一', '更新说明二'),
        manifest=manifest or {},
        assets=tuple(assets))


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

    def prepare_update(self, _release, output=print):
        self.prepared = True
        output('下载进度：100%')
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


class UpdaterTests(unittest.TestCase):
    def test_semantic_version_comparison(self):
        self.assertTrue(is_newer('0.5.1', '0.5.0'))
        self.assertFalse(is_newer('v0.5.0', '0.5.0'))

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


if __name__ == '__main__':
    unittest.main()
