from __future__ import annotations

import importlib.util
from pathlib import Path

from xynigo_auth.system_order_key import create_system_order_key, is_system_order_key


def test_service_and_database_migration_generate_the_same_ok1_key() -> None:
    expected = "OK1-EKF8Q-ZHK40-NMC1C-ZKEH0"
    assert create_system_order_key("测试店铺", "GSH-DEMO", "XMWU-DEMO") == expected
    assert is_system_order_key(expected)

    migration_path = (
        Path(__file__).resolve().parents[1]
        / "migrations"
        / "versions"
        / "0016_system_order_key_v1.py"
    )
    spec = importlib.util.spec_from_file_location("migration_0016", migration_path)
    assert spec is not None and spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)

    assert migration._system_order_key(  # type: ignore[attr-defined]
        "  测试店铺  ", " gsh-demo ", " xmwu-demo "
    ) == expected
