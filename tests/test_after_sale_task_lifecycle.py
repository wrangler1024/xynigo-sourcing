import subprocess
from pathlib import Path


def test_after_sale_task_lifecycle_race_regression():
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        ['node', 'tests/fixtures/after_sale_task_lifecycle_ui.cjs'],
        cwd=root, capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
