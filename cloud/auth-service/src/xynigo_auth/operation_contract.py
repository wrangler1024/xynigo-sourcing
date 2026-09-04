"""Credential-free contracts for daily local execution results."""

from __future__ import annotations

from datetime import datetime
from typing import Literal
import uuid

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)


SAFE_KEY_RE = r"^[A-Za-z0-9._:-]+$"
MASK_MARKERS = frozenset({"*", "·", "•"})


def _single_line(value: str) -> str:
    return " ".join(value.split())


def _timezone_required(value: datetime | None) -> datetime | None:
    if value is not None and (value.tzinfo is None or value.utcoffset() is None):
        raise ValueError("datetime must contain a timezone")
    return value


class PurchaserAllocationSummary(BaseModel):
    """Credential-free assignment used to create an environment run."""

    model_config = ConfigDict(extra="forbid")

    purchaserLabel: str = Field(min_length=1, max_length=100)
    count: int = Field(ge=1, le=2000)

    @field_validator("purchaserLabel")
    @classmethod
    def normalize_purchaser(cls, value: str) -> str:
        normalized = _single_line(value)
        if any(character in normalized for character in (",", ":")):
            raise ValueError("purchaserLabel contains an unsupported separator")
        return normalized


class EnvironmentCreationRunCreateBody(BaseModel):
    """Safe cloud request for a durable environment-creation Run."""

    model_config = ConfigDict(extra="forbid")

    idempotencyKey: str = Field(min_length=8, max_length=128, pattern=SAFE_KEY_RE)
    executorId: uuid.UUID
    mode: Literal["bound", "backup", "test"] = "bound"
    site: Literal["US", "MX"]
    purchaseDate: str = Field(pattern=r"^20\d{6}$")
    environmentGroup: str = Field(min_length=1, max_length=12)
    cloudPlanId: str | None = Field(
        default=None,
        validation_alias=AliasChoices("cloudPlanId", "planRef"),
        min_length=8,
        max_length=128,
        pattern=SAFE_KEY_RE,
    )
    buyerLabel: str | None = Field(default=None, min_length=1, max_length=100)
    totalCount: int = Field(ge=1, le=2000)
    verifySampleCount: int = Field(default=0, ge=0, le=2000)
    assignments: list[PurchaserAllocationSummary] = Field(
        default_factory=list, max_length=100
    )

    @field_validator("environmentGroup", "buyerLabel")
    @classmethod
    def normalize_create_text(cls, value: str | None) -> str | None:
        return _single_line(value) if value is not None else None

    @model_validator(mode="after")
    def validate_mode_inputs(self) -> "EnvironmentCreationRunCreateBody":
        if self.verifySampleCount > self.totalCount:
            raise ValueError("verifySampleCount cannot exceed totalCount")
        if self.mode == "bound":
            if not self.cloudPlanId:
                raise ValueError("bound environment run requires cloudPlanId")
            if not self.assignments:
                raise ValueError("bound environment run requires assignments")
            if sum(item.count for item in self.assignments) != self.totalCount:
                raise ValueError("assignment count must equal totalCount")
        elif not self.buyerLabel:
            raise ValueError("backup/test environment run requires buyerLabel")
        return self


class EnvironmentRetryRunCreateBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    idempotencyKey: str = Field(min_length=8, max_length=128, pattern=SAFE_KEY_RE)
    retryMode: Literal["single", "failed"]
    accountRefs: list[str] = Field(min_length=1, max_length=2000)
    takeover: bool = False
    executorId: uuid.UUID | None = None
    cloudPlanId: str | None = Field(
        default=None,
        min_length=8,
        max_length=128,
        pattern=SAFE_KEY_RE,
    )

    @field_validator("accountRefs")
    @classmethod
    def validate_retry_refs(cls, value: list[str]) -> list[str]:
        normalized = [_single_line(item) for item in value]
        if any(
            not item or len(item) > 128
            or not all(character.isalnum() or character in "._:-" for character in item)
            for item in normalized
        ):
            raise ValueError("retry account reference is invalid")
        if len(normalized) != len(set(normalized)):
            raise ValueError("accountRefs must be unique")
        return normalized

    @model_validator(mode="after")
    def validate_retry_mode(self) -> "EnvironmentRetryRunCreateBody":
        if self.retryMode == "single" and len(self.accountRefs) != 1:
            raise ValueError("single retry requires exactly one account")
        takeover_fields = self.executorId is not None or self.cloudPlanId is not None
        if self.takeover and (self.executorId is None or not self.cloudPlanId):
            raise ValueError("takeover requires executorId and cloudPlanId")
        if not self.takeover and takeover_fields:
            raise ValueError("takeover fields require takeover=true")
        return self


class EnvironmentPlanParseBody(BaseModel):
    """Upload request for cloud parsing into an encrypted short-lived plan."""

    model_config = ConfigDict(extra="forbid")

    idempotencyKey: str = Field(min_length=8, max_length=128, pattern=SAFE_KEY_RE)
    filename: str = Field(min_length=1, max_length=255)
    contentBase64: str = Field(min_length=1, max_length=28 * 1024 * 1024)
    site: Literal["US", "MX"]
    environmentGroup: str = Field(min_length=1, max_length=12)

    @field_validator("filename")
    @classmethod
    def validate_filename(cls, value: str) -> str:
        normalized = _single_line(value)
        if any(character in normalized for character in ("/", "\\", "\x00")):
            raise ValueError("filename must not contain a path")
        if not normalized.casefold().endswith(".xlsx"):
            raise ValueError("environment plan must be an xlsx workbook")
        return normalized

    @field_validator("environmentGroup")
    @classmethod
    def validate_environment_group(cls, value: str) -> str:
        return _single_line(value)


class EnvironmentPlanPreviewItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    emailMasked: str = Field(min_length=1, max_length=255)
    orderMasked: str = Field(default="", max_length=160)
    cookieBytes: int = Field(ge=0, le=10 * 1024 * 1024)

    @field_validator("emailMasked")
    @classmethod
    def require_masked_preview_account(cls, value: str) -> str:
        normalized = _single_line(value)
        if "@" in normalized and not any(marker in normalized for marker in MASK_MARKERS):
            raise ValueError("emailMasked must be masked")
        return normalized


class EnvironmentPlanParseResult(BaseModel):
    """Credential-free result returned by the cloud workbook parser."""

    model_config = ConfigDict(extra="forbid")

    cloudPlanId: str = Field(
        validation_alias=AliasChoices("cloudPlanId", "planId"),
        min_length=8,
        max_length=128,
        pattern=SAFE_KEY_RE,
    )
    site: Literal["US", "MX"]
    environmentGroup: str | None = Field(default=None, min_length=1, max_length=12)
    count: int = Field(ge=1, le=2000)
    cookieCount: int = Field(ge=0, le=2000)
    mixedSiteCookieCount: int = Field(default=0, ge=0, le=2000)
    passwordKindCount: int = Field(ge=0, le=2000)
    duplicateCount: int = Field(ge=0, le=2000)
    issueCount: int = Field(ge=0, le=2000)
    orderCount: int = Field(ge=0, le=2000)
    expiresAt: datetime | None = None
    runtime: Literal["cloud", "local"] = "local"
    reused: bool = False
    preview: list[EnvironmentPlanPreviewItem] = Field(default_factory=list, max_length=5)

    _validate_expiry = field_validator("expiresAt")(_timezone_required)

    @model_validator(mode="after")
    def validate_counts(self) -> "EnvironmentPlanParseResult":
        bounded = (
            self.cookieCount,
            self.mixedSiteCookieCount,
            self.duplicateCount,
            self.issueCount,
            self.orderCount,
        )
        if any(value > self.count for value in bounded):
            raise ValueError("environment parse count exceeds total")
        return self


class EnvironmentPlanDryRunBody(BaseModel):
    """Schedule an encrypted, read-only preview on one local executor."""

    model_config = ConfigDict(extra="forbid")

    idempotencyKey: str = Field(min_length=8, max_length=128, pattern=SAFE_KEY_RE)
    executorId: uuid.UUID
    site: Literal["US", "MX"]
    purchaseDate: str = Field(pattern=r"^20\d{6}$")
    environmentGroup: str = Field(min_length=1, max_length=12)
    totalCount: int = Field(ge=1, le=2000)
    assignments: list[PurchaserAllocationSummary] = Field(
        min_length=1, max_length=100
    )

    @field_validator("environmentGroup")
    @classmethod
    def normalize_group(cls, value: str) -> str:
        return _single_line(value)

    @model_validator(mode="after")
    def validate_assignment_total(self) -> "EnvironmentPlanDryRunBody":
        if sum(item.count for item in self.assignments) != self.totalCount:
            raise ValueError("assignment count must equal totalCount")
        return self


class EnvironmentWorkspacePreferenceBody(BaseModel):
    """Cloud-owned last selection; partial site-tag updates are allowed."""

    model_config = ConfigDict(extra="forbid")

    purchaseSite: Literal["US", "MX"]
    purchaseTags: dict[Literal["US", "MX"], str] = Field(default_factory=dict)

    @field_validator("purchaseTags")
    @classmethod
    def validate_partial_purchase_tags(
        cls, value: dict[str, str]
    ) -> dict[str, str]:
        normalized = {key: _single_line(item) for key, item in value.items()}
        if any(not item or len(item) > 12 for item in normalized.values()):
            raise ValueError("purchase group is invalid")
        return normalized


class WorkspaceBuyerItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=100)
    code: str = Field(min_length=1, max_length=16, pattern=r"^[A-Z0-9_-]+$")


class WorkspaceEnvironmentPreferences(BaseModel):
    model_config = ConfigDict(extra="forbid")

    purchaseSite: Literal["US", "MX"]
    purchaseTags: dict[Literal["US", "MX"], str]
    importBuyerPlan: str = Field(default="", max_length=1000)
    verifySampleCount: int = Field(ge=0, le=10)
    buyers: list[WorkspaceBuyerItem] = Field(min_length=1, max_length=20)
    buyerDefaultSplit: list[str] = Field(default_factory=list, max_length=20)
    backupMaxCount: int = Field(ge=1, le=2000)

    @field_validator("purchaseTags")
    @classmethod
    def validate_purchase_tags(
        cls, value: dict[str, str]
    ) -> dict[str, str]:
        if set(value) != {"US", "MX"}:
            raise ValueError("purchaseTags must contain US and MX")
        normalized = {key: _single_line(item) for key, item in value.items()}
        if any(len(item) > 255 for item in normalized.values()):
            raise ValueError("purchase group is too long")
        return normalized

    @field_validator("buyerDefaultSplit")
    @classmethod
    def validate_default_split(cls, value: list[str]) -> list[str]:
        normalized = [_single_line(item) for item in value]
        if any(not item or len(item) > 100 for item in normalized):
            raise ValueError("buyerDefaultSplit is invalid")
        if len(normalized) != len(set(normalized)):
            raise ValueError("buyerDefaultSplit must be unique")
        return normalized


class WorkspacePreflightSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ready: bool
    hubConnected: bool
    groupFound: bool
    proxyConfigured: bool
    purchaseTag: str = Field(default="", max_length=12)
    configuredWorkers: int = Field(ge=1, le=10)
    effectiveWorkers: int = Field(ge=1, le=10)
    message: str = Field(default="", max_length=300)

    @field_validator("purchaseTag", "message")
    @classmethod
    def normalize_preflight_text(cls, value: str) -> str:
        return _single_line(value)


class WorkspaceRuntimeConfig(BaseModel):
    """Credential-free device settings safe to display while a Run is active."""

    model_config = ConfigDict(extra="forbid")

    configRevision: str = Field(pattern=r"^[a-f0-9]{64}$")
    hubPort: int = Field(ge=1, le=65535)
    concurrency: int = Field(ge=1, le=5)
    envCreateWorkers: int = Field(ge=1, le=10)
    verifySampleCount: int = Field(ge=0, le=10)
    safeParallelTasks: bool
    queryBrowserMode: Literal["headless", "visible"] = "headless"


class ExecutorWorkspaceSnapshotResult(BaseModel):
    """Strict non-sensitive snapshot used for fast cloud workspace restore."""

    model_config = ConfigDict(extra="forbid")

    schemaVersion: Literal[1] = 1
    snapshotRevision: str = Field(pattern=r"^[a-f0-9]{64}$")
    capturedAt: datetime
    preferences: WorkspaceEnvironmentPreferences
    runtimeConfig: WorkspaceRuntimeConfig | None = None
    groups: list[str] = Field(default_factory=list, max_length=500)
    preflight: dict[Literal["US", "MX"], WorkspacePreflightSnapshot]

    @field_validator("capturedAt")
    @classmethod
    def validate_snapshot_timezone(cls, value: datetime) -> datetime:
        return _timezone_required(value)  # type: ignore[return-value]

    @field_validator("groups")
    @classmethod
    def validate_groups(cls, value: list[str]) -> list[str]:
        normalized = [_single_line(item) for item in value]
        if any(not item or len(item) > 255 for item in normalized):
            raise ValueError("workspace group is invalid")
        if len(normalized) != len(set(normalized)):
            raise ValueError("workspace groups must be unique")
        return normalized

    @field_validator("preflight")
    @classmethod
    def validate_preflight_sites(
        cls, value: dict[str, WorkspacePreflightSnapshot]
    ) -> dict[str, WorkspacePreflightSnapshot]:
        if set(value) != {"US", "MX"}:
            raise ValueError("preflight must contain US and MX")
        return value


class LogisticsQueryRunCreateBody(BaseModel):
    """Safe cloud request for a durable logistics-query Run."""

    model_config = ConfigDict(extra="forbid")

    idempotencyKey: str = Field(min_length=8, max_length=128, pattern=SAFE_KEY_RE)
    executorId: uuid.UUID
    queryMode: Literal["initial", "single_retry", "failed_retry"] = "initial"
    browserMode: Literal["default", "headless", "visible"] = "default"
    allowOpenEnvironment: bool = False
    parentRunId: uuid.UUID | None = None
    force: bool = False
    site: Literal["US", "MX"]
    environmentSerials: list[str] = Field(min_length=1, max_length=2000)

    @field_validator("environmentSerials")
    @classmethod
    def normalize_environment_serials(cls, value: list[str]) -> list[str]:
        normalized = [_single_line(item) for item in value]
        if any(not item or len(item) > 64 for item in normalized):
            raise ValueError("environment serial is invalid")
        if len(normalized) != len(set(normalized)):
            raise ValueError("environmentSerials must be unique")
        return normalized

    @model_validator(mode="after")
    def validate_query_mode(self) -> "LogisticsQueryRunCreateBody":
        if self.queryMode == "single_retry" and len(self.environmentSerials) != 1:
            raise ValueError("single_retry requires exactly one environment")
        if self.queryMode != "single_retry" and self.force:
            raise ValueError("force is only supported for single_retry")
        if self.queryMode == "initial" and self.parentRunId is not None:
            raise ValueError("initial query cannot have a parent run")
        if self.queryMode != "initial" and self.parentRunId is None:
            raise ValueError("retry query requires a parent run")
        return self


class WorkspaceViewPreferenceBody(BaseModel):
    """Generic table/view presentation settings, scoped to one signed-in user."""

    model_config = ConfigDict(extra="forbid")

    schemaVersion: int = Field(default=1, ge=1, le=20)
    visibleFields: list[str] = Field(min_length=1, max_length=64)
    fieldOrder: list[str] = Field(default_factory=list, max_length=64)

    @field_validator("visibleFields", "fieldOrder")
    @classmethod
    def validate_field_keys(cls, value: list[str]) -> list[str]:
        normalized = [_single_line(item) for item in value]
        if any(
            not item or len(item) > 64
            or not all(character.isalnum() or character in "._-" for character in item)
            for item in normalized
        ):
            raise ValueError("view field key is invalid")
        if len(normalized) != len(set(normalized)):
            raise ValueError("view field keys must be unique")
        return normalized

    @model_validator(mode="after")
    def validate_field_order(self) -> "WorkspaceViewPreferenceBody":
        if self.fieldOrder and set(self.fieldOrder) != set(self.visibleFields):
            raise ValueError("fieldOrder must contain the visible fields")
        return self


class EnvironmentCreationResultItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    accountRef: str = Field(min_length=8, max_length=128, pattern=SAFE_KEY_RE)
    accountLabel: str = Field(min_length=1, max_length=255)
    purchaserLabel: str = Field(min_length=1, max_length=100)
    environmentName: str = Field(min_length=1, max_length=255)
    environmentRef: str | None = Field(
        default=None, min_length=1, max_length=128, pattern=SAFE_KEY_RE
    )
    environmentSerial: str | None = Field(default=None, min_length=1, max_length=64)
    status: Literal["success", "failed", "stopped"]
    errorStep: str = Field(default="", max_length=64)
    errorSummary: str = Field(default="", max_length=300)
    bindingAt: datetime | None = None
    recoveredExisting: bool = False
    createdInRun: bool = False
    cleanupStatus: Literal[
        "not_required", "pending", "deleting", "deleted", "failed"
    ] = "not_required"
    cleanupErrorCode: str = Field(
        default="", max_length=128, pattern=r"^[A-Za-z0-9._:-]*$"
    )
    cleanupErrorSummary: str = Field(default="", max_length=300)

    @field_validator(
        "accountLabel",
        "purchaserLabel",
        "environmentName",
        "environmentSerial",
        "errorStep",
        "errorSummary",
        "cleanupErrorCode",
        "cleanupErrorSummary",
    )
    @classmethod
    def normalize_text(cls, value: str | None) -> str | None:
        return _single_line(value) if value is not None else None

    @field_validator("accountLabel")
    @classmethod
    def require_masked_account(cls, value: str) -> str:
        if "@" in value and not any(marker in value for marker in MASK_MARKERS):
            raise ValueError("accountLabel must be masked")
        return value

    @field_validator("bindingAt")
    @classmethod
    def validate_binding_timezone(cls, value: datetime | None) -> datetime | None:
        return _timezone_required(value)

    @model_validator(mode="after")
    def successful_result_has_environment_identity(
        self,
    ) -> "EnvironmentCreationResultItem":
        if self.status == "success" and (
            not self.environmentRef or not self.environmentSerial
        ):
            raise ValueError(
                "successful environment result requires reference and serial"
            )
        return self


class EnvironmentRunProgressItem(BaseModel):
    """Credential-free row snapshot accepted from a formal executor task."""

    model_config = ConfigDict(extra="forbid")

    accountRef: str = Field(min_length=8, max_length=128, pattern=SAFE_KEY_RE)
    accountLabel: str = Field(min_length=1, max_length=255)
    purchaserLabel: str = Field(min_length=1, max_length=100)
    environmentName: str = Field(min_length=1, max_length=255)
    environmentRef: str | None = Field(
        default=None, min_length=1, max_length=128, pattern=SAFE_KEY_RE
    )
    environmentSerial: str | None = Field(default=None, min_length=1, max_length=64)
    status: Literal["queued", "running", "success", "failed", "stopped"]
    currentStep: str = Field(default="", max_length=64)
    completedSteps: list[str] = Field(default_factory=list, max_length=20)
    errorStep: str = Field(default="", max_length=64)
    errorSummary: str = Field(default="", max_length=300)
    recoveredExisting: bool = False
    createdInRun: bool = False
    cleanupStatus: Literal[
        "not_required", "pending", "deleting", "deleted", "failed"
    ] = "not_required"
    cleanupErrorCode: str = Field(
        default="", max_length=128, pattern=r"^[A-Za-z0-9._:-]*$"
    )
    cleanupErrorSummary: str = Field(default="", max_length=300)
    ipAddress: str = Field(default="", max_length=64)
    ipCountry: str = Field(default="", max_length=100)
    ipErrorCode: str = Field(
        default="", max_length=128, pattern=r"^[A-Za-z0-9._:-]*$"
    )
    ipErrorSummary: str = Field(default="", max_length=300)
    ipVerified: bool | None = None

    @field_validator(
        "accountLabel",
        "purchaserLabel",
        "environmentName",
        "environmentSerial",
        "currentStep",
        "errorStep",
        "errorSummary",
        "cleanupErrorCode",
        "cleanupErrorSummary",
        "ipCountry",
        "ipErrorCode",
        "ipErrorSummary",
    )
    @classmethod
    def normalize_progress_text(cls, value: str | None) -> str | None:
        return _single_line(value) if value is not None else None

    @field_validator("accountLabel")
    @classmethod
    def require_progress_masked_account(cls, value: str) -> str:
        if "@" in value and not any(marker in value for marker in MASK_MARKERS):
            raise ValueError("accountLabel must be masked")
        return value

    @field_validator("completedSteps")
    @classmethod
    def normalize_completed_steps(cls, value: list[str]) -> list[str]:
        normalized = [_single_line(item) for item in value]
        if any(not item or len(item) > 64 for item in normalized):
            raise ValueError("completed step is invalid")
        if len(normalized) != len(set(normalized)):
            raise ValueError("completedSteps must be unique")
        return normalized


class EnvironmentIpCheckItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    environmentName: str = Field(min_length=1, max_length=255)
    ipAddress: str = Field(default="", max_length=64)
    country: str = Field(default="", max_length=100)
    city: str = Field(default="", max_length=100)
    isp: str = Field(default="", max_length=200)
    ok: bool
    errorCode: str = Field(
        default="", max_length=128, pattern=r"^[A-Za-z0-9._:-]*$"
    )
    errorSummary: str = Field(default="", max_length=300)

    @field_validator(
        "environmentName", "country", "city", "isp", "errorCode", "errorSummary"
    )
    @classmethod
    def normalize_text(cls, value: str) -> str:
        return _single_line(value)


class EnvironmentCreationRunBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: Literal["local_executor"] = "local_executor"
    runKey: str = Field(min_length=8, max_length=128, pattern=SAFE_KEY_RE)
    site: Literal["US", "MX"]
    purchaseDate: str = Field(pattern=r"^20\d{6}$")
    environmentGroup: str = Field(min_length=1, max_length=255)
    startedAt: datetime | None = None
    completedAt: datetime
    results: list[EnvironmentCreationResultItem] = Field(min_length=1, max_length=2000)
    ipChecks: list[EnvironmentIpCheckItem] = Field(default_factory=list, max_length=2000)

    @field_validator("environmentGroup")
    @classmethod
    def normalize_group(cls, value: str) -> str:
        return _single_line(value)

    @field_validator("startedAt", "completedAt")
    @classmethod
    def validate_timezone(cls, value: datetime | None) -> datetime | None:
        return _timezone_required(value)

    @model_validator(mode="after")
    def unique_rows(self) -> "EnvironmentCreationRunBody":
        refs = [item.accountRef for item in self.results]
        if len(refs) != len(set(refs)):
            raise ValueError("accountRef must be unique in one run")
        names = [item.environmentName for item in self.ipChecks]
        if len(names) != len(set(names)):
            raise ValueError("environmentName must be unique in ipChecks")
        return self


class LogisticsQueryResultItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    environmentSerial: str = Field(min_length=1, max_length=64)
    environmentName: str = Field(default="", max_length=255)
    status: Literal["ok", "fail", "login", "inuse", "stopped", "pending"]
    platformOrderNo: str = Field(default="", max_length=160)
    orderTime: str = Field(default="", max_length=64)
    amount: str = Field(default="", max_length=64)
    platformStatus: str = Field(default="", max_length=100)
    statusLabel: str = Field(default="", max_length=100)
    fulfillmentStage: str = Field(default="", max_length=100)
    trackingNumbers: list[str] = Field(default_factory=list, max_length=20)
    packageNumbers: list[str] = Field(default_factory=list, max_length=20)
    carrier: str = Field(default="", max_length=100)
    firstTrackingAt: datetime | None = None
    firstTrackingTime: str = Field(default="", max_length=64)
    firstTrackingSummary: str = Field(default="", max_length=300)
    firstTrackingLeadMinutes: int | None = Field(
        default=None, ge=0, le=527040
    )
    cancelled: bool = False
    riskOrder: bool = False
    riskSummary: str = Field(default="", max_length=300)
    ipAddress: str = Field(default="", max_length=64)
    timeZone: str = Field(default="", max_length=100)
    utcOffsetMinutes: int | None = Field(default=None, ge=-840, le=840)
    queriedAt: datetime | None = None
    errorSummary: str = Field(default="", max_length=300)
    screenshotStatus: str = Field(default="", max_length=32)

    @field_validator(
        "environmentSerial",
        "environmentName",
        "platformOrderNo",
        "orderTime",
        "amount",
        "platformStatus",
        "statusLabel",
        "fulfillmentStage",
        "carrier",
        "firstTrackingTime",
        "firstTrackingSummary",
        "riskSummary",
        "timeZone",
        "errorSummary",
        "screenshotStatus",
    )
    @classmethod
    def normalize_text(cls, value: str) -> str:
        return _single_line(value)

    @field_validator("trackingNumbers", "packageNumbers")
    @classmethod
    def normalize_numbers(cls, value: list[str]) -> list[str]:
        normalized = [_single_line(item)[:200] for item in value if _single_line(item)]
        if len(normalized) != len(set(normalized)):
            raise ValueError("tracking and package numbers must be unique")
        return normalized

    @field_validator("queriedAt")
    @classmethod
    def validate_query_timezone(cls, value: datetime | None) -> datetime | None:
        return _timezone_required(value)

    @field_validator("firstTrackingAt")
    @classmethod
    def validate_first_tracking_timezone(cls, value: datetime | None) -> datetime | None:
        return _timezone_required(value)


class LogisticsRunProgressItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    environmentSerial: str = Field(min_length=1, max_length=64)
    environmentName: str = Field(default="", max_length=255)
    status: Literal["pending", "running", "ok", "fail", "login", "inuse", "stopped"]
    currentStep: str = Field(default="", max_length=64)
    completedSteps: list[str] = Field(default_factory=list, max_length=20)
    platformOrderNo: str = Field(default="", max_length=160)
    orderTime: str = Field(default="", max_length=64)
    amount: str = Field(default="", max_length=64)
    platformStatus: str = Field(default="", max_length=100)
    statusLabel: str = Field(default="", max_length=100)
    fulfillmentStage: str = Field(default="", max_length=100)
    trackingNumbers: list[str] = Field(default_factory=list, max_length=20)
    packageNumbers: list[str] = Field(default_factory=list, max_length=20)
    carrier: str = Field(default="", max_length=100)
    firstTrackingAt: datetime | None = None
    firstTrackingTime: str = Field(default="", max_length=64)
    firstTrackingSummary: str = Field(default="", max_length=300)
    firstTrackingLeadMinutes: int | None = Field(
        default=None, ge=0, le=527040
    )
    cancelled: bool = False
    riskOrder: bool = False
    riskSummary: str = Field(default="", max_length=300)
    ipAddress: str = Field(default="", max_length=64)
    timeZone: str = Field(default="", max_length=100)
    utcOffsetMinutes: int | None = Field(default=None, ge=-840, le=840)
    queriedAt: datetime | None = None
    errorSummary: str = Field(default="", max_length=300)
    screenshotStatus: str = Field(default="", max_length=32)
    screenshotSizeKb: int = Field(default=0, ge=0, le=1024)

    @field_validator(
        "environmentSerial",
        "environmentName",
        "currentStep",
        "platformOrderNo",
        "orderTime",
        "amount",
        "platformStatus",
        "statusLabel",
        "fulfillmentStage",
        "carrier",
        "firstTrackingTime",
        "firstTrackingSummary",
        "riskSummary",
        "ipAddress",
        "timeZone",
        "errorSummary",
        "screenshotStatus",
    )
    @classmethod
    def normalize_logistics_progress_text(cls, value: str) -> str:
        return _single_line(value)

    @field_validator("completedSteps", "trackingNumbers", "packageNumbers")
    @classmethod
    def normalize_progress_lists(cls, value: list[str]) -> list[str]:
        normalized = [_single_line(item) for item in value if _single_line(item)]
        if any(len(item) > 200 for item in normalized):
            raise ValueError("progress list item is too long")
        if len(normalized) != len(set(normalized)):
            raise ValueError("progress list items must be unique")
        return normalized

    @field_validator("queriedAt")
    @classmethod
    def validate_progress_query_timezone(cls, value: datetime | None) -> datetime | None:
        return _timezone_required(value)

    @field_validator("firstTrackingAt")
    @classmethod
    def validate_progress_first_tracking_timezone(
        cls, value: datetime | None
    ) -> datetime | None:
        return _timezone_required(value)


class LogisticsScreenshotProgressItem(BaseModel):
    """Short-lived screenshot attachment uploaded with logistics progress."""

    model_config = ConfigDict(extra="forbid")

    environmentSerial: str = Field(min_length=1, max_length=64)
    contentType: Literal["image/jpeg"] = "image/jpeg"
    contentBase64: str = Field(min_length=4, max_length=460_000)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    size: int = Field(ge=1, le=350 * 1024)


class LogisticsQueryRunBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: Literal["local_executor"] = "local_executor"
    runKey: str = Field(min_length=8, max_length=128, pattern=SAFE_KEY_RE)
    queryMode: Literal["initial", "single_retry", "failed_retry"]
    site: Literal["US", "MX"]
    startedAt: datetime | None = None
    completedAt: datetime
    results: list[LogisticsQueryResultItem] = Field(min_length=1, max_length=2000)

    @field_validator("startedAt", "completedAt")
    @classmethod
    def validate_timezone(cls, value: datetime | None) -> datetime | None:
        return _timezone_required(value)

    @model_validator(mode="after")
    def unique_rows(self) -> "LogisticsQueryRunBody":
        serials = [item.environmentSerial for item in self.results]
        if len(serials) != len(set(serials)):
            raise ValueError("environmentSerial must be unique in one run")
        return self
