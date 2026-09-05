"""Strict wire contracts for the cloud-to-local executor P1 channel.

The device credential travels only in the Authorization header. Payloads are
deliberately small. Legacy config read/write contracts remain parseable during
the upgrade window, while executors advertising ``local.config.desktop.v1``
reject them and report only the strict ``config.summary.v2`` allowlist.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


PAIRING_CODE_PATTERN = re.compile(r"^[A-HJ-NP-Z2-9]{8}$")
REVISION_PATTERN = re.compile(r"^[a-f0-9]{64}$")
STABLE_CODE_PATTERN = re.compile(r"^[a-z][a-z0-9_.-]{1,127}$")
EXECUTOR_CAPABILITIES = Literal[
    "config.read.v1",
    "config.write.v1",
    "config.summary.v2",
    "local.config.desktop.v1",
    "workspace.rpc.v1",
    "workspace.snapshot.v1",
    "environment.parse.v1",
    "environment.cloud-plan.v1",
    "environment.cloud-inventory.v1",
    "environment.preview-bound.v1",
    "logistics.query.v1",
    "logistics.auto-site.v1",
    "environment.create-bound.v1",
    "environment.create-backup.v1",
    "environment.retry-row.v1",
    "environment.retry-failed.v1",
]


class StrictBody(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PairingCodeCreateBody(StrictBody):
    displayNameHint: str | None = Field(default=None, max_length=128)

    @field_validator("displayNameHint")
    @classmethod
    def normalize_hint(cls, value: str | None) -> str | None:
        normalized = " ".join(str(value or "").split())
        return normalized or None


class ExecutorPairBody(StrictBody):
    pairingCode: str = Field(min_length=8, max_length=11)
    displayName: str = Field(min_length=1, max_length=128)
    platform: Literal["windows", "macos"]
    architecture: Literal["x86_64", "arm64"]
    clientVersion: str = Field(min_length=1, max_length=64)
    protocolVersion: int = Field(default=1, ge=1, le=10)
    capabilities: list[EXECUTOR_CAPABILITIES] = Field(
        default_factory=lambda: ["config.read.v1", "config.write.v1"],
        max_length=16,
    )
    devicePublicKey: str | None = Field(default=None, max_length=8192)

    @field_validator("pairingCode")
    @classmethod
    def normalize_code(cls, value: str) -> str:
        normalized = re.sub(r"[\s-]", "", value).upper()
        if not PAIRING_CODE_PATTERN.fullmatch(normalized):
            raise ValueError("pairing code format is invalid")
        return normalized

    @field_validator("displayName", "clientVersion")
    @classmethod
    def normalize_text(cls, value: str) -> str:
        normalized = " ".join(value.split())
        if not normalized:
            raise ValueError("must not be blank")
        return normalized

    @field_validator("capabilities")
    @classmethod
    def unique_capabilities(cls, value: list[str]) -> list[str]:
        return sorted(set(value))


class ExecutorRuntimeConfigSummary(StrictBody):
    hubPort: int = Field(ge=1, le=65535)
    concurrency: int = Field(ge=1, le=5)
    envCreateWorkers: int = Field(ge=1, le=10)
    verifySampleCount: int = Field(ge=0, le=10)
    safeParallelTasks: bool
    queryBrowserMode: Literal["headless", "visible"] = "headless"
    queryAllowOpenEnvironment: bool = False


class ExecutorConfiguredSummary(StrictBody):
    hubApiKey: bool
    larkAppCredentials: bool
    larkLegacyTarget: bool
    purchaseAssistantDataSources: bool
    teamDefaultDataSource: bool


class ExecutorDataSourceSummary(StrictBody):
    dataSourceCount: int = Field(ge=0, le=10000)
    buyerProfileCount: int = Field(ge=0, le=10000)
    environmentBindingCount: int = Field(ge=0, le=100000)
    pendingOwnerConfirmationCount: int = Field(ge=0, le=10000)
    mappingConflictCount: int = Field(ge=0, le=10000)


class ExecutorComplianceSummary(StrictBody):
    status: Literal["ready", "degraded"]
    issueCodes: list[str] = Field(default_factory=list, max_length=16)

    @field_validator("issueCodes")
    @classmethod
    def validate_issue_codes(cls, value: list[str]) -> list[str]:
        normalized = sorted(set(str(item or "").strip() for item in value))
        if any(not STABLE_CODE_PATTERN.fullmatch(item) for item in normalized):
            raise ValueError("summary issue code format is invalid")
        return normalized


class ExecutorConfigSummaryV2(StrictBody):
    schemaVersion: Literal[2]
    configRevision: str
    capturedAt: datetime
    runtimeConfig: ExecutorRuntimeConfigSummary
    configured: ExecutorConfiguredSummary
    dataSources: ExecutorDataSourceSummary
    compliance: ExecutorComplianceSummary

    @field_validator("configRevision")
    @classmethod
    def validate_config_revision(cls, value: str) -> str:
        normalized = value.strip().lower()
        if not REVISION_PATTERN.fullmatch(normalized):
            raise ValueError("config summary revision format is invalid")
        return normalized

    @model_validator(mode="after")
    def validate_summary_consistency(self) -> "ExecutorConfigSummaryV2":
        if self.capturedAt.tzinfo is None:
            raise ValueError("config summary capturedAt must include timezone")
        has_sources = self.dataSources.dataSourceCount > 0
        if self.configured.purchaseAssistantDataSources != has_sources:
            raise ValueError("config summary data-source readiness is inconsistent")
        if self.configured.teamDefaultDataSource and not has_sources:
            raise ValueError("config summary team default requires a data source")
        has_issues = bool(self.compliance.issueCodes)
        if (self.compliance.status == "ready") == has_issues:
            raise ValueError("config summary compliance status is inconsistent")
        return self


class ExecutorPollBody(StrictBody):
    waitSeconds: int = Field(default=25, ge=0, le=25)
    acceptTasks: bool = True
    configRevision: str | None = None
    hubStatus: Literal["unknown", "ready", "offline", "limited"] = "unknown"
    clientVersion: str = Field(min_length=1, max_length=64)
    protocolVersion: int = Field(default=1, ge=1, le=10)
    capabilities: list[EXECUTOR_CAPABILITIES] = Field(
        default_factory=list,
        max_length=16,
    )
    configSummary: ExecutorConfigSummaryV2 | None = None

    @field_validator("configRevision")
    @classmethod
    def validate_revision(cls, value: str | None) -> str | None:
        normalized = str(value or "").strip().lower()
        if not normalized:
            return None
        if not REVISION_PATTERN.fullmatch(normalized):
            raise ValueError("config revision format is invalid")
        return normalized

    @field_validator("clientVersion")
    @classmethod
    def normalize_client_version(cls, value: str) -> str:
        return " ".join(value.split())

    @field_validator("capabilities")
    @classmethod
    def unique_capabilities(cls, value: list[str]) -> list[str]:
        return sorted(set(value))

    @model_validator(mode="after")
    def require_summary_capability(self) -> "ExecutorPollBody":
        if self.configSummary is not None and "config.summary.v2" not in self.capabilities:
            raise ValueError("config summary capability is required")
        return self


class ExecutorConfigWriteBody(StrictBody):
    expectedRevision: str
    config: dict[str, Any]
    idempotencyKey: str | None = Field(default=None, min_length=8, max_length=128)

    @field_validator("expectedRevision")
    @classmethod
    def validate_revision(cls, value: str) -> str:
        normalized = value.strip().lower()
        if not REVISION_PATTERN.fullmatch(normalized):
            raise ValueError("expected revision format is invalid")
        return normalized

    @field_validator("config")
    @classmethod
    def validate_safe_config(cls, value: dict[str, Any]) -> dict[str, Any]:
        allowed = {
            "hubPort",
            "concurrency",
            "verifySampleCount",
            "envCreateWorkers",
            "safeParallelTasks",
            "queryBrowserMode",
            "queryAllowOpenEnvironment",
        }
        if set(value) - allowed:
            raise ValueError("config contains unsupported or sensitive fields")
        integer_ranges = {
            "hubPort": (1, 65535),
            "concurrency": (1, 5),
            "verifySampleCount": (0, 10),
            "envCreateWorkers": (1, 10),
        }
        for key, (minimum, maximum) in integer_ranges.items():
            if key not in value:
                continue
            item = value[key]
            if isinstance(item, bool) or not isinstance(item, int):
                raise ValueError(f"{key} must be an integer")
            if item < minimum or item > maximum:
                raise ValueError(f"{key} is outside the supported range")
        if "safeParallelTasks" in value and not isinstance(
            value["safeParallelTasks"], bool
        ):
            raise ValueError("safeParallelTasks must be a boolean")
        if "queryBrowserMode" in value and value["queryBrowserMode"] not in {
            "headless",
            "visible",
        }:
            raise ValueError("queryBrowserMode is invalid")
        if "queryAllowOpenEnvironment" in value and not isinstance(
            value["queryAllowOpenEnvironment"], bool
        ):
            raise ValueError("queryAllowOpenEnvironment must be a boolean")
        # Keep the task envelope bounded before it reaches the durable queue.
        if len(str(value)) > 32_000:
            raise ValueError("config payload is too large")
        return value


class ExecutorWorkspaceRpcBody(StrictBody):
    method: Literal["GET", "POST"]
    path: str = Field(min_length=5, max_length=2048)
    body: dict[str, Any] | None = None
    idempotencyKey: str | None = Field(default=None, min_length=8, max_length=128)

    @field_validator("path")
    @classmethod
    def validate_local_path(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized.startswith("/api/") or normalized.startswith("//"):
            raise ValueError("workspace RPC path must be a local API path")
        if any(character in normalized for character in ("\r", "\n", "#")):
            raise ValueError("workspace RPC path contains invalid characters")
        return normalized

    @model_validator(mode="after")
    def validate_body_and_size(self) -> "ExecutorWorkspaceRpcBody":
        if self.method == "GET" and self.body not in (None, {}):
            raise ValueError("GET workspace RPC cannot contain a body")
        if len(str(self.body or {})) > 28 * 1024 * 1024:
            raise ValueError("workspace RPC body is too large")
        return self


class ExecutorWorkspaceSnapshotRefreshBody(StrictBody):
    idempotencyKey: str | None = Field(default=None, min_length=8, max_length=128)


class ExecutorTaskStartBody(StrictBody):
    leaseToken: str = Field(min_length=32, max_length=256)


class ExecutorEnvironmentPreviewRow(StrictBody):
    emailMasked: str = Field(min_length=1, max_length=255)
    purchaserLabel: str = Field(min_length=1, max_length=100)
    environmentName: str = Field(min_length=1, max_length=255)
    recoveredExisting: bool = False

    @field_validator("emailMasked", "purchaserLabel", "environmentName")
    @classmethod
    def normalize_text(cls, value: str) -> str:
        normalized = " ".join(value.split())
        if not normalized:
            raise ValueError("preview text must not be blank")
        return normalized

    @field_validator("emailMasked")
    @classmethod
    def require_masked_email(cls, value: str) -> str:
        if "@" in value and not any(
            marker in value for marker in ("*", "•", "…")
        ):
            raise ValueError("preview email must be masked")
        return value


class ExecutorHubEnvironmentObservation(StrictBody):
    environmentKey: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    environmentName: str = Field(min_length=1, max_length=255)
    environmentRef: str | None = Field(default=None, min_length=1, max_length=128)
    environmentSerial: str | None = Field(default=None, min_length=1, max_length=64)
    environmentGroup: str = Field(default="", max_length=255)
    site: Literal["US", "MX"] | None = None
    sourceOrderRef: str | None = Field(
        default=None, pattern=r"^sha256:[a-f0-9]{64}$"
    )

    @field_validator("environmentName")
    @classmethod
    def normalize_observation_name(cls, value: str) -> str:
        normalized = " ".join(value.split())
        if not normalized:
            raise ValueError("environmentName must not be blank")
        return normalized

    @field_validator("environmentRef", "environmentSerial")
    @classmethod
    def normalize_optional_observation_text(
        cls, value: str | None
    ) -> str | None:
        normalized = " ".join(str(value or "").split())
        return normalized or None

    @field_validator("environmentGroup")
    @classmethod
    def normalize_observation_group(cls, value: str) -> str:
        return " ".join(value.split())


class ExecutorHubEnvironmentSnapshot(StrictBody):
    snapshotRevision: str = Field(pattern=r"^[a-f0-9]{64}$")
    capturedAt: datetime
    environmentCount: int = Field(ge=0, le=50000)
    rows: list[ExecutorHubEnvironmentObservation] = Field(max_length=50000)

    @model_validator(mode="after")
    def validate_snapshot(self) -> "ExecutorHubEnvironmentSnapshot":
        if self.capturedAt.tzinfo is None or self.capturedAt.utcoffset() is None:
            raise ValueError("capturedAt must contain a timezone")
        if self.environmentCount != len(self.rows):
            raise ValueError("environmentCount must match rows")
        keys = [row.environmentKey for row in self.rows]
        if len(keys) != len(set(keys)):
            raise ValueError("environment snapshot keys must be unique")
        return self


class ExecutorEnvironmentPreviewResult(StrictBody):
    valid: Literal[True]
    count: int = Field(ge=1, le=2000)
    rows: list[ExecutorEnvironmentPreviewRow] = Field(
        min_length=1, max_length=2000
    )
    inventorySource: Literal["hubstudio", "cloud_cache"] = "hubstudio"
    inventoryCapturedAt: datetime | None = None
    inventorySnapshot: ExecutorHubEnvironmentSnapshot | None = None

    @model_validator(mode="after")
    def validate_rows(self) -> "ExecutorEnvironmentPreviewResult":
        if self.count != len(self.rows):
            raise ValueError("preview count must match rows")
        names = [row.environmentName for row in self.rows]
        if len(names) != len(set(names)):
            raise ValueError("preview environment names must be unique")
        if (
            self.inventoryCapturedAt is not None
            and (
                self.inventoryCapturedAt.tzinfo is None
                or self.inventoryCapturedAt.utcoffset() is None
            )
        ):
            raise ValueError("inventoryCapturedAt must contain a timezone")
        return self


class ExecutorTaskLeaseBody(ExecutorTaskStartBody):
    pass


class ExecutorTaskProgressBody(ExecutorTaskStartBody):
    phase: str = Field(min_length=1, max_length=64)
    current: int | None = Field(default=None, ge=0)
    total: int | None = Field(default=None, ge=0)
    stableCode: str | None = Field(default=None, max_length=128)
    snapshot: dict[str, Any] | None = None

    @field_validator("phase")
    @classmethod
    def normalize_phase(cls, value: str) -> str:
        normalized = value.strip().lower()
        if not STABLE_CODE_PATTERN.fullmatch(normalized):
            raise ValueError("phase format is invalid")
        return normalized

    @field_validator("stableCode")
    @classmethod
    def validate_stable_code(cls, value: str | None) -> str | None:
        normalized = str(value or "").strip().lower()
        if normalized and not STABLE_CODE_PATTERN.fullmatch(normalized):
            raise ValueError("stable code format is invalid")
        return normalized or None

    @model_validator(mode="after")
    def validate_progress(self) -> "ExecutorTaskProgressBody":
        if self.current is not None and self.total is not None and self.current > self.total:
            raise ValueError("progress current cannot exceed total")
        if self.snapshot is not None:
            raw = str(self.snapshot)
            if len(raw) > 2 * 1024 * 1024:
                raise ValueError("progress snapshot is too large")
        return self


class ExecutorTaskFinishBody(ExecutorTaskStartBody):
    outcome: Literal["succeeded", "failed"]
    resultCode: str = Field(min_length=2, max_length=128)
    resultSummary: dict[str, Any] = Field(default_factory=dict)

    @field_validator("resultCode")
    @classmethod
    def validate_result_code(cls, value: str) -> str:
        normalized = value.strip().lower()
        if not STABLE_CODE_PATTERN.fullmatch(normalized):
            raise ValueError("result code format is invalid")
        return normalized

    @field_validator("resultSummary")
    @classmethod
    def validate_result_summary(cls, value: dict[str, Any]) -> dict[str, Any]:
        if len(str(value)) > 48 * 1024 * 1024:
            raise ValueError("result summary is too large")
        # Workspace RPC results are encrypted before durable storage. Config
        # tasks still receive a strict field-level validation in the service.
        return value


class ExecutorTaskCancelBody(StrictBody):
    expectedStatus: Literal["queued", "leased", "running", "cancel_requested"] | None = None


def task_uuid(value: str | uuid.UUID) -> uuid.UUID:
    return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))
