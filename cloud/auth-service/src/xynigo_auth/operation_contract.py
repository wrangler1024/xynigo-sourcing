"""Credential-free contracts for daily local execution results."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


SAFE_KEY_RE = r"^[A-Za-z0-9._:-]+$"
MASK_MARKERS = frozenset({"*", "·", "•"})


def _single_line(value: str) -> str:
    return " ".join(value.split())


def _timezone_required(value: datetime | None) -> datetime | None:
    if value is not None and (value.tzinfo is None or value.utcoffset() is None):
        raise ValueError("datetime must contain a timezone")
    return value


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
    status: Literal["success", "failed"]
    errorStep: str = Field(default="", max_length=64)
    errorSummary: str = Field(default="", max_length=300)
    bindingAt: datetime | None = None
    recoveredExisting: bool = False

    @field_validator(
        "accountLabel",
        "purchaserLabel",
        "environmentName",
        "environmentSerial",
        "errorStep",
        "errorSummary",
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


class EnvironmentIpCheckItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    environmentName: str = Field(min_length=1, max_length=255)
    ipAddress: str = Field(default="", max_length=64)
    country: str = Field(default="", max_length=100)
    city: str = Field(default="", max_length=100)
    isp: str = Field(default="", max_length=200)
    ok: bool
    errorSummary: str = Field(default="", max_length=300)

    @field_validator("environmentName", "country", "city", "isp", "errorSummary")
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
