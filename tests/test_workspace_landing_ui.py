"""Verify initial landing uses permissions without opening unrelated business pages."""
from pathlib import Path
import shutil
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]


class WorkspaceLandingUiTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('node'), 'Node.js is required')
    def test_permission_aware_workbench_landing(self):
        result = subprocess.run(['node', 'tests/fixtures/workspace_landing_ui.cjs'],
                                cwd=ROOT, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
