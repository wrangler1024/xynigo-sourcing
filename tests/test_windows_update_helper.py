# -*- coding: utf-8 -*-
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from purchase_tool.updater import MANAGED_PATHS


@unittest.skipUnless(os.name == 'nt', 'Windows PowerShell integration test')
class WindowsUpdateHelperTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='xynigo-helper-test-'))
        self.install = self.tmp / 'install'
        self.stage = self.tmp / 'stage'
        self.backup = self.tmp / 'backup'
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

    def _run(self, fail_after=0):
        return subprocess.run([
            'powershell.exe', '-NoLogo', '-NoProfile',
            '-ExecutionPolicy', 'Bypass', '-File', str(self.helper),
            '-InstallDir', str(self.install),
            '-StageDir', str(self.stage),
            '-BackupDir', str(self.backup),
            '-SkipWait', '-NoRestart',
            '-TestFailAfterInstall', str(fail_after),
        ], capture_output=True, text=True, encoding='utf-8',
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


if __name__ == '__main__':
    unittest.main()
