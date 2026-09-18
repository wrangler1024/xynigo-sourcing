"""Run the real browser workflow with synthetic scan/claim responses only."""
from pathlib import Path
import subprocess


def test_environment_submission_is_single_pass_direct_write():
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(['node', 'tests/fixtures/after_sale_submit_environments_ui.cjs'],
                            cwd=root, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
