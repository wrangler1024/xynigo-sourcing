"""Credential-safe contracts for tenant-managed external integrations."""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

from .tenant_feishu import ALLOWED_PROXY_PERMISSIONS


class FeishuIntegrationWriteBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expectedRevision: int = Field(ge=0)
    appId: str = Field(min_length=8, max_length=128)
    appSecret: SecretStr = Field(min_length=8, max_length=512)

    @field_validator("appId")
    @classmethod
    def validate_app_id(cls, value: str) -> str:
        normalized = value.strip()
        if not re.fullmatch(r"cli_[A-Za-z0-9]{6,124}", normalized):
            raise ValueError("Feishu app ID format is invalid")
        return normalized


class FeishuReadProxyBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    permission: Literal[
        "assistant.access",
        "resource.store.read",
        "resource.ip.read",
        "system.lark_connection.manage",
    ]
    path: str = Field(min_length=10, max_length=512)
    query: dict[str, str] = Field(default_factory=dict, max_length=12)

    @field_validator("permission")
    @classmethod
    def validate_permission(cls, value: str) -> str:
        if value not in ALLOWED_PROXY_PERMISSIONS:
            raise ValueError("Feishu proxy permission is invalid")
        return value

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized.startswith("/open-apis/") or any(
            marker in normalized for marker in ("?", "#", "\r", "\n")
        ):
            raise ValueError("Feishu proxy path is invalid")
        return normalized
