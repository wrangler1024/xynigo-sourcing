"""Environment outcomes remain visible without becoming actionable order records."""
from pathlib import Path
import subprocess


def test_after_sale_environment_rows_lifecycle():
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        ['node', 'tests/fixtures/after_sale_environment_rows_ui.cjs'],
        cwd=root, capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
