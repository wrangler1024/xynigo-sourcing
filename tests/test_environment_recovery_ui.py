from pathlib import Path
import shutil
import subprocess

import pytest


@pytest.mark.skipif(not shutil.which('node'), reason='Node.js required')
def test_shipped_environment_recovery_handlers():
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(['node', 'tests/fixtures/environment_recovery_ui.cjs'],
                            cwd=root, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
