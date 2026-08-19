# -*- coding: utf-8 -*-
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import unittest

from purchase_tool.updater import MANAGED_PATHS


@unittest.skipUnless(os.name == 'nt', 'Windows PowerShell integration test')
class WindowsUpdateHelperTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='xynigo-helper-test-'))
        self.install = self.tmp / 'install'
        self.stage = self.tmp / 'stage'
        self.backup = self.tmp / 'backup'
        self.state_dir = self.tmp / 'state'
        self.install.mkdir()
        self.stage.mkdir()
        root = Path(__file__).resolve().parents[1]
        source = root / 'packaging' / 'windows' / 'update-helper.ps1'
        self.helper = self.tmp / 'update-helper.ps1'
        self.helper.write_text(
            source.read_text(encoding='utf-8'), encoding='utf-8-sig')
        for name in MANAGED_PATHS:
            self._write_managed(self.install / name, 'old-' + name)
            self._write_managed(self.stage / name, 'new-' + name)
        (self.install / 'config.json').write_text(
            '{"keep": true}', encoding='utf-8')
        (self.install / '查询日志').mkdir()
        (self.install / '查询日志' / 'history.html').write_text(
            'keep-log', encoding='utf-8')
        (self.install / '运行数据').mkdir()
        (self.install / '运行数据' / 'state.json').write_text(
            'keep-data', encoding='utf-8')
        (self.install / 'imports').mkdir()
        (self.install / 'imports' / 'user.xlsx').write_bytes(b'keep-import')

    def tearDown(self):
        shutil.rmtree(str(self.tmp), ignore_errors=True)

    @staticmethod
    def _write_managed(path, content):
        if '.' in path.name:
            path.write_text(content, encoding='utf-8')
        else:
            path.mkdir(parents=True)
            (path / 'marker.txt').write_text(content, encoding='utf-8')

    @staticmethod
    def _read_managed(path):
        if path.is_dir():
            return (path / 'marker.txt').read_text(encoding='utf-8')
        return path.read_text(encoding='utf-8')

    def _run(self, fail_after=0, no_restart=True):
        command = [
            'powershell.exe', '-NoLogo', '-NoProfile',
            '-ExecutionPolicy', 'Bypass', '-File', str(self.helper),
            '-InstallDir', str(self.install),
            '-StageDir', str(self.stage),
            '-BackupDir', str(self.backup),
            '-StateDir', str(self.state_dir),
            '-SkipWait',
            '-TestFailAfterInstall', str(fail_after),
        ]
        if no_restart:
            command.append('-NoRestart')
        return subprocess.run(
           command, capture_output=True, text=True, encoding='utf-8',
           errors='replace', timeout=90)

    def _assert_preserved(self):
        self.assertEqual(
            (self.install / 'config.json').read_text(encoding='utf-8'),
            '{"keep": true}')
        self.assertEqual(
            (self.install / '查询日志' / 'history.html').read_text(
                encoding='utf-8'), 'keep-log')
        self.assertEqual(
            (self.install / '运行数据' / 'state.json').read_text(
                encoding='utf-8'), 'keep-data')
        self.assertEqual(
            (self.install / 'imports' / 'user.xlsx').read_bytes(),
            b'keep-import')

    def test_success_replaces_program_and_preserves_user_files(self):
        result = self._run()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        for name in MANAGED_PATHS:
            self.assertEqual(self._read_managed(self.install / name),
                             'new-' + name)
            self.assertEqual(self._read_managed(self.backup / name),
                             'old-' + name)
        self._assert_preserved()

    def test_replacement_failure_rolls_back_program(self):
        result = self._run(fail_after=3)
        self.assertNotEqual(result.returncode, 0)
        for name in MANAGED_PATHS:
            self.assertEqual(self._read_managed(self.install / name),
                             'old-' + name)
        self._assert_preserved()

    def test_success_restarts_new_version_with_one_time_skip(self):
        launcher = (
            '@echo off\r\n'
            'echo restarted> "%~dp0restart-marker.txt"\r\n'
        )
        (self.stage / '启动.bat').write_text(
            launcher, encoding='ascii', newline='')
        result = self._run(no_restart=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        marker = self.install / 'restart-marker.txt'
        deadline = time.time() + 10
        while time.time() < deadline and not marker.exists():
            time.sleep(0.1)
        self.assertTrue(marker.is_file(), '新版本启动器未被拉起')
        self.assertEqual(
            marker.read_text(encoding='utf-8-sig').strip(), 'restarted')
        skip_marker = self.state_dir / 'skip-update-once'
        self.assertTrue(skip_marker.is_file())
        self.assertEqual(
            skip_marker.read_text(encoding='ascii').strip(), '1')


if __name__ == '__main__':
    unittest.main()
