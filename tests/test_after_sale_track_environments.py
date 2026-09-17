"""Run the production frontend handlers with synthetic history responses."""
from pathlib import Path
import subprocess


def test_environment_tracking_ui_workflow():
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(['node', 'tests/fixtures/after_sale_track_environments_ui.cjs'],
                            cwd=root, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
