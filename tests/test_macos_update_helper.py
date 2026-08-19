# -*- coding: utf-8 -*-
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

from purchase_tool.updater import MACOS_MANAGED_PATHS


@unittest.skipUnless(sys.platform == 'darwin', 'macOS integration test')
class MacOSUpdateHelperTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.tmp = Path(self.temp_dir.name)
        self.install = self.tmp / 'install'
        self.stage = self.tmp / 'stage'
        self.backup = self.tmp / 'backup'
        self.state_dir = self.tmp / 'state'
        self.install.mkdir()
        self.stage.mkdir()
        root = Path(__file__).resolve().parents[1]
        self.helper = root / 'packaging' / 'macos' / 'update-helper.sh'
        for name in MACOS_MANAGED_PATHS:
            self._write_managed(self.install, name, 'old-' + name)
            self._write_managed(self.stage, name, 'new-' + name)
        self.user_files = {
            'config.json': '{"hubPort": 6873}',
            'data/local.json': 'local-data',
            'logs/query.log': 'local-log',
            'imports/orders.xlsx': 'user-import',
        }
        for name, content in self.user_files.items():
            path = self.install / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding='utf-8')

    def tearDown(self):
        self.temp_dir.cleanup()

    @staticmethod
    def _write_managed(root, name, content):
        path = root / name
        if name == 'runtime':
            path.mkdir(parents=True, exist_ok=True)
            (path / 'xynigo-sourcing').write_text(content, encoding='utf-8')
        elif '.' in Path(name).name or name.endswith('.command'):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding='utf-8')
        else:
            path.mkdir(parents=True, exist_ok=True)
            (path / 'version.txt').write_text(content, encoding='utf-8')

    def _run(self, fail_after=0, restart=False):
        command = [
            '/bin/bash', str(self.helper),
            '--install-dir', str(self.install),
            '--stage-dir', str(self.stage),
            '--backup-dir', str(self.backup),
            '--state-dir', str(self.state_dir),
            '--skip-wait',
            '--test-fail-after-install', str(fail_after),
        ]
        if restart:
            command.append('--test-restart-direct')
        else:
            command.append('--no-restart')
        return subprocess.run(
            command, check=False, capture_output=True, text=True,
            env=dict(os.environ, HOME=str(self.tmp)))

    def _assert_user_files_preserved(self):
        for name, content in self.user_files.items():
            self.assertEqual(
                (self.install / name).read_text(encoding='utf-8'), content)

    def test_success_replaces_program_and_preserves_user_files(self):
        result = self._run()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        for name in MACOS_MANAGED_PATHS:
            path = self.install / name
            target = path if path.is_file() else path / 'xynigo-sourcing'
            self.assertEqual(
                target.read_text(encoding='utf-8'), 'new-' + name)
        self._assert_user_files_preserved()

    def test_replacement_failure_rolls_back_program(self):
        result = self._run(fail_after=2)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        for name in MACOS_MANAGED_PATHS:
            path = self.install / name
            target = path if path.is_file() else path / 'xynigo-sourcing'
            self.assertEqual(
                target.read_text(encoding='utf-8'), 'old-' + name)
        self._assert_user_files_preserved()

    def test_success_restarts_new_version_with_one_time_skip(self):
        launcher = (
            '#!/bin/bash\n'
            'echo restarted > "$(dirname "$0")/restart-marker.txt"\n'
        )
        (self.stage / '启动-Mac.command').write_text(
            launcher, encoding='utf-8')
        result = self._run(restart=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        marker = self.install / 'restart-marker.txt'
        deadline = time.time() + 5
        while time.time() < deadline and not marker.exists():
            time.sleep(0.1)
        self.assertTrue(marker.is_file(), '新版本启动器未被拉起')
        self.assertEqual(marker.read_text(encoding='utf-8').strip(), 'restarted')
        skip_marker = self.state_dir / 'skip-update-once'
        self.assertEqual(
            skip_marker.read_text(encoding='ascii').strip(), '1')


if __name__ == '__main__':
    unittest.main()
