"""云端服务配置：全部从环境变量读取，前缀为 XYNIGO_AUTH_。

例如 XYNIGO_AUTH_DATABASE_URL 对应字段 database_url。密码类用 SecretStr，打印日志时不会明文出现。
"""

from __future__ import annotations

from functools import cached_property
from typing import Literal
from urllib.parse import urlparse

from pydantic import SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """启动时校验必填项；production 还要求 HTTPS 回调、Secure Cookie、租户与 Host 白名单。"""

    model_config = SettingsConfigDict(env_prefix="XYNIGO_AUTH_", extra="ignore")

    environment: Literal["development", "test", "production"] = "production"
    database_url: SecretStr
    buyer_credential_encryption_key: SecretStr = SecretStr("")
    feishu_app_id: str
    feishu_app_secret: SecretStr
    feishu_redirect_uri: str
    feishu_pkce_method: Literal["S256", "plain", "disabled"] = "S256"
    allowed_tenant_keys: str = ""
    bootstrap_super_admin_open_ids: str = ""
    auto_activate_users: bool = False
    session_ttl_seconds: int = 8 * 60 * 60
    oauth_attempt_ttl_seconds: int = 5 * 60
    local_login_ttl_seconds: int = 5 * 60
    system_log_retention_days: int = 30
    system_log_max_rows_per_tenant: int = 100_000
    system_log_runtime_sample_rate: float = 1.0
    feishu_operation_sync_enabled: bool = False
    feishu_operation_base_token: str = ""
    feishu_buyer_account_table_id: str = ""
    feishu_environment_result_table_id: str = ""
    feishu_logistics_result_table_id: str = ""
    feishu_operation_sync_interval_seconds: int = 15
    feishu_purchase_sync_enabled: bool = False
    feishu_purchase_base_token: str = ""
    feishu_purchase_order_table_id: str = ""
    feishu_purchase_line_table_id: str = ""
    feishu_purchase_sync_interval_seconds: int = 15
    cookie_name: str = "xynigo_session"
    cookie_secure: bool = True
    login_success_path: str = "/"
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

    @field_validator("buyer_credential_encryption_key")
    @classmethod
    def normalize_buyer_credential_key(cls, value: SecretStr) -> SecretStr:
        return SecretStr(value.get_secret_value().strip())

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

    @field_validator("local_login_ttl_seconds")
    @classmethod
    def validate_local_login_ttl(cls, value: int) -> int:
        if value < 60 or value > 600:
            raise ValueError("local_login_ttl_seconds must be between 60 and 600")
        return value

    @field_validator("system_log_retention_days")
    @classmethod
    def validate_system_log_retention_days(cls, value: int) -> int:
        if value < 1 or value > 365:
            raise ValueError("system_log_retention_days must be between 1 and 365")
        return value

    @field_validator("system_log_max_rows_per_tenant")
    @classmethod
    def validate_system_log_max_rows(cls, value: int) -> int:
        if value < 1_000 or value > 10_000_000:
            raise ValueError(
                "system_log_max_rows_per_tenant must be between 1000 and 10000000"
            )
        return value

    @field_validator("system_log_runtime_sample_rate")
    @classmethod
    def validate_system_log_sample_rate(cls, value: float) -> float:
        if value < 0 or value > 1:
            raise ValueError("system_log_runtime_sample_rate must be between 0 and 1")
        return value

    @field_validator(
        "feishu_operation_sync_interval_seconds",
        "feishu_purchase_sync_interval_seconds",
    )
    @classmethod
    def validate_operation_sync_interval(cls, value: int) -> int:
        if value < 5 or value > 3600:
            raise ValueError(
                "feishu_operation_sync_interval_seconds must be between 5 and 3600"
            )
        return value

    @model_validator(mode="after")
    def validate_operation_sync_target(self) -> "Settings":
        if self.feishu_operation_sync_enabled and not all(
            (
                self.feishu_operation_base_token.strip(),
                self.feishu_buyer_account_table_id.strip(),
                self.feishu_environment_result_table_id.strip(),
                self.feishu_logistics_result_table_id.strip(),
            )
        ):
            raise ValueError(
                "enabled Feishu operation sync requires base and all table identifiers"
            )
        return self

    @model_validator(mode="after")
    def validate_purchase_sync_target(self) -> "Settings":
        if self.feishu_purchase_sync_enabled and not all(
            (
                self.feishu_purchase_base_token.strip(),
                self.feishu_purchase_order_table_id.strip(),
                self.feishu_purchase_line_table_id.strip(),
            )
        ):
            raise ValueError(
                "enabled Feishu purchase sync requires base and both table identifiers"
            )
        return self

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
            if not self.buyer_credential_encryption_key.get_secret_value():
                raise ValueError(
                    "production requires buyer credential application encryption"
                )
        return self

    @cached_property
    def allowed_tenant_key_set(self) -> frozenset[str]:
        """允许登录的飞书企业 tenant_key 列表。"""
        return frozenset(part.strip() for part in self.allowed_tenant_keys.split(",") if part.strip())

    @cached_property
    def bootstrap_super_admin_open_id_set(self) -> frozenset[str]:
        """启动时指定的超级管理员飞书 open_id，不会「第一个登录的人自动成管理员」。"""
        return frozenset(
            part.strip() for part in self.bootstrap_super_admin_open_ids.split(",") if part.strip()
        )

    @cached_property
    def allowed_host_list(self) -> list[str]:
        """HTTP Host 白名单，生产环境不能为 *。"""
        return [part.strip() for part in self.allowed_hosts.split(",") if part.strip()]
