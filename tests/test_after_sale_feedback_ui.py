"""Exercise production Web functions with synthetic responses only."""
import subprocess
from pathlib import Path


def test_feedback_ui_and_real_discovery_tracking_handoffs():
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(['node', 'tests/fixtures/after_sale_feedback_ui.cjs'],
                            cwd=root, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
