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


def test_settings_repr_redacts_database_password_and_app_secret() -> None:
    settings = production_settings()
    rendered = repr(settings)
    assert "password" not in rendered
    assert "test-secret-not-real" not in rendered


def test_production_rejects_wildcard_hosts() -> None:
    with pytest.raises(ValidationError):
        production_settings(allowed_hosts="*")
