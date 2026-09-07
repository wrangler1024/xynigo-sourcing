"""HTTP contracts for encrypted organization data-source synchronization."""

from __future__ import annotations

import json
from typing import Any

from pydantic import Field, field_validator

from .executor_contract import StrictBody


class DataSourceRegistryPublishBody(StrictBody):
    expectedRevision: int = Field(ge=0)
    registry: dict[str, Any]

    @field_validator("registry")
    @classmethod
    def validate_registry_size(cls, value: dict[str, Any]) -> dict[str, Any]:
        if len(json.dumps(value, ensure_ascii=False)) > 2 * 1024 * 1024:
            raise ValueError("data-source registry is too large")
        return value
