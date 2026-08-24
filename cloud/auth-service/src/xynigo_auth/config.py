from __future__ import annotations

from functools import cached_property
from typing import Literal
from urllib.parse import urlparse

from pydantic import SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="XYNIGO_AUTH_", extra="ignore")

    environment: Literal["development", "test", "production"] = "production"
    database_url: SecretStr
    feishu_app_id: str
    feishu_app_secret: SecretStr
    feishu_redirect_uri: str
    feishu_pkce_method: Literal["S256", "plain", "disabled"] = "S256"
    allowed_tenant_keys: str = ""
    bootstrap_super_admin_open_ids: str = ""
    auto_activate_users: bool = False
    session_ttl_seconds: int = 8 * 60 * 60
    oauth_attempt_ttl_seconds: int = 5 * 60
    cookie_name: str = "xynigo_session"
    cookie_secure: bool = True
    login_success_path: str = "/v1/auth/me"
    allowed_hosts: str = "localhost,127.0.0.1"

    @field_validator("feishu_app_id", "feishu_redirect_uri")
    @classmethod
    def required_values_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank")
        return value.strip()

    @field_validator("database_url", "feishu_app_secret")
    @classmethod
    def secrets_must_not_be_blank(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value().strip():
            raise ValueError("must not be blank")
        return value

    @field_validator("session_ttl_seconds")
    @classmethod
    def validate_session_ttl(cls, value: int) -> int:
        if value < 300 or value > 7 * 24 * 60 * 60:
            raise ValueError("session_ttl_seconds must be between 300 and 604800")
        return value

    @field_validator("oauth_attempt_ttl_seconds")
    @classmethod
    def validate_oauth_ttl(cls, value: int) -> int:
        if value < 60 or value > 300:
            raise ValueError("oauth_attempt_ttl_seconds must be between 60 and 300")
        return value

    @field_validator("login_success_path")
    @classmethod
    def validate_success_path(cls, value: str) -> str:
        if not value.startswith("/") or value.startswith("//") or urlparse(value).scheme:
            raise ValueError("login_success_path must be a same-origin absolute path")
        return value

    @model_validator(mode="after")
    def validate_production_security(self) -> "Settings":
        redirect = urlparse(self.feishu_redirect_uri)
        if self.environment == "production":
            if redirect.scheme != "https":
                raise ValueError("production feishu_redirect_uri must use HTTPS")
            if not self.cookie_secure:
                raise ValueError("production cookies must be Secure")
            if not self.allowed_tenant_key_set:
                raise ValueError("production requires at least one allowed tenant key")
            if not self.allowed_host_list or "*" in self.allowed_host_list:
                raise ValueError("production requires an explicit allowed host list")
        return self

    @cached_property
    def allowed_tenant_key_set(self) -> frozenset[str]:
        return frozenset(part.strip() for part in self.allowed_tenant_keys.split(",") if part.strip())

    @cached_property
    def bootstrap_super_admin_open_id_set(self) -> frozenset[str]:
        return frozenset(
            part.strip() for part in self.bootstrap_super_admin_open_ids.split(",") if part.strip()
        )

    @cached_property
    def allowed_host_list(self) -> list[str]:
        return [part.strip() for part in self.allowed_hosts.split(",") if part.strip()]
