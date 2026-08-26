from __future__ import annotations

import pytest
from pydantic import ValidationError

from xynigo_auth.config import Settings


def production_settings(**overrides):
    values = {
        "environment": "production",
        "database_url": "postgresql+psycopg://user:password@db/xynigo",
        "feishu_app_id": "cli_test",
        "feishu_app_secret": "test-secret-not-real",
        "buyer_credential_encryption_key": (
            "MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA="
        ),
        "feishu_redirect_uri": "https://xynigo.example.com/v1/auth/feishu/callback",
        "allowed_tenant_keys": "tenant_allowed",
        "cookie_secure": True,
        "allowed_hosts": "xynigo.example.com,127.0.0.1",
    }
    values.update(overrides)
    return Settings(**values)


def test_production_requires_https_callback() -> None:
    with pytest.raises(ValidationError):
        production_settings(feishu_redirect_uri="http://xynigo.example.com/callback")


def test_production_requires_tenant_allowlist() -> None:
    with pytest.raises(ValidationError):
        production_settings(allowed_tenant_keys="")


def test_success_redirect_cannot_leave_origin() -> None:
    with pytest.raises(ValidationError):
        production_settings(login_success_path="https://attacker.example/capture")


def test_settings_repr_redacts_database_password_app_secret_and_buyer_key() -> None:
    settings = production_settings()
    rendered = repr(settings)
    assert "password" not in rendered
    assert "test-secret-not-real" not in rendered
    assert "MDAwMDAwMDAwMDAw" not in rendered


def test_production_rejects_wildcard_hosts() -> None:
    with pytest.raises(ValidationError):
        production_settings(allowed_hosts="*")


def test_system_log_retention_capacity_and_sampling_are_bounded() -> None:
    settings = production_settings()
    assert settings.system_log_retention_days == 30
    assert settings.system_log_max_rows_per_tenant == 100_000
    assert settings.system_log_runtime_sample_rate == 1.0
    for invalid in (
        {"system_log_retention_days": 0},
        {"system_log_retention_days": 366},
        {"system_log_max_rows_per_tenant": 999},
        {"system_log_runtime_sample_rate": -0.01},
        {"system_log_runtime_sample_rate": 1.01},
    ):
        with pytest.raises(ValidationError):
            production_settings(**invalid)


def test_enabled_purchase_sync_requires_complete_base_coordinates() -> None:
    with pytest.raises(ValidationError):
        production_settings(feishu_purchase_sync_enabled=True)
    settings = production_settings(
        feishu_purchase_sync_enabled=True,
        feishu_purchase_base_token="RzcSyntheticBaseToken",
        feishu_purchase_order_table_id="tblSyntheticMaster",
        feishu_purchase_line_table_id="tblSyntheticLine",
    )
    assert settings.feishu_purchase_sync_enabled is True
