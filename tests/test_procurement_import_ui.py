"""Execute the production import handlers with synthetic DOM/API boundaries."""
from pathlib import Path
import shutil
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[1]


class ProcurementImportUiTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('node'), 'Node.js is required for UI state checks')
    def test_real_handlers_preserve_validation_confirmation_and_busy_states(self):
        result = subprocess.run(
            ['node', str(ROOT / 'tests/fixtures/procurement_import_ui.cjs')],
            cwd=ROOT, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
