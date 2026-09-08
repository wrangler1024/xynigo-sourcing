"""Run real permission/entry handlers using synthetic identities."""
from pathlib import Path
import shutil
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]


class PluginAccessUiTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('node'), 'Node.js is required')
    def test_plugin_only_identity_cannot_enter_workspace(self):
        result = subprocess.run(['node', 'tests/fixtures/plugin_access_ui.cjs'],
                                cwd=ROOT, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
