"""Buyer-account snapshot contracts for metadata and encrypted credentials."""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


SAFE_REF_RE = r"^[A-Za-z0-9._:-]+$"
MASK_MARKERS = frozenset({"*", "·", "•"})


def _normalized_text(value: str) -> str:
    return " ".join(value.split())


class BuyerAccountSnapshotItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    accountRef: str = Field(min_length=8, max_length=128, pattern=SAFE_REF_RE)
    displayLabel: str = Field(min_length=1, max_length=255)
    site: Literal["US", "MX"]
    availabilityStatus: Literal["available", "manual_review", "disabled"] = (
        "available"
    )
    credentialStatus: Literal["ready", "unverified", "invalid", "unknown"] = (
        "unknown"
    )
    sourceStatus: str = Field(default="", max_length=64)
    sourceVendorLabel: str = Field(default="", max_length=100)
    sourceBatchRef: str = Field(default="", max_length=128)
    sourcePurchaseDate: date | None = None
    sourceOrderRef: str | None = Field(
        default=None, min_length=8, max_length=128, pattern=SAFE_REF_RE
    )
    hubEnvironmentRef: str | None = Field(
        default=None, min_length=1, max_length=128, pattern=SAFE_REF_RE
    )
    hubEnvironmentName: str = Field(default="", max_length=255)
    operatorLabel: str = Field(default="", max_length=100)
    sourceUpdatedAt: datetime | None = None
    credentials: "BuyerAccountCredentials | None" = None
    businessProfile: "BuyerAccountBusinessProfile" = Field(
        default_factory=lambda: BuyerAccountBusinessProfile()
    )

    @field_validator(
        "displayLabel",
        "sourceStatus",
        "sourceVendorLabel",
        "sourceBatchRef",
        "hubEnvironmentName",
        "operatorLabel",
    )
    @classmethod
    def normalize_labels(cls, value: str) -> str:
        return _normalized_text(value)

    @field_validator("displayLabel")
    @classmethod
    def reject_full_account_identifier(cls, value: str) -> str:
        if "@" in value and not any(marker in value for marker in MASK_MARKERS):
            raise ValueError("displayLabel must be masked")
        return value

    @field_validator("sourceUpdatedAt")
    @classmethod
    def timezone_required(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("sourceUpdatedAt must contain a timezone")
        return value

    @model_validator(mode="after")
    def environment_pair(self) -> "BuyerAccountSnapshotItem":
        if bool(self.hubEnvironmentRef) != bool(self.hubEnvironmentName):
            raise ValueError("Hub environment reference and name must be provided together")
        return self


class BuyerAccountCredentials(BaseModel):
    model_config = ConfigDict(extra="forbid")

    accountIdentifier: str = Field(min_length=1, max_length=320)
    phoneNumber: str = Field(default="", max_length=64)
    password: str = Field(default="", max_length=1024)
    cookie: str = Field(default="", max_length=2_000_000)
    verificationKey: str = Field(default="", max_length=1024)
    verificationKeyLink: str = Field(default="", max_length=4096)
    loginLink: str = Field(default="", max_length=4096)

    @field_validator(
        "accountIdentifier",
        "phoneNumber",
        "password",
        "cookie",
        "verificationKey",
        "verificationKeyLink",
        "loginLink",
    )
    @classmethod
    def strip_outer_whitespace(cls, value: str) -> str:
        return value.strip()


class BuyerAccountBusinessProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    bindingTime: datetime | None = None
    firstLoginAt: datetime | None = None
    bindingEnvironment: str = Field(default="", max_length=255)
    abnormalRecord: str = Field(default="", max_length=20_000)
    note: str = Field(default="", max_length=20_000)
    sourcePurchaseOrderNo: str = Field(default="", max_length=255)
    sourceOperators: list[str] = Field(default_factory=list, max_length=50)
    lastUsedAt: datetime | None = None
    environmentSequence: int | None = Field(default=None, ge=0, le=10_000_000)
    environmentGroupName: str = Field(default="", max_length=255)
    sourceAccountId: str = Field(default="", max_length=128)
    cumulativeOrderCount: float | None = Field(default=None, ge=0)
    migrationStatus: str = Field(default="", max_length=100)
    sourceCreatedAt: datetime | None = None
    sourceCreatedBy: str = Field(default="", max_length=255)

    @field_validator(
        "bindingEnvironment",
        "abnormalRecord",
        "note",
        "sourcePurchaseOrderNo",
        "environmentGroupName",
        "sourceAccountId",
        "migrationStatus",
        "sourceCreatedBy",
    )
    @classmethod
    def normalize_business_text(cls, value: str) -> str:
        return value.strip()

    @field_validator("sourceOperators")
    @classmethod
    def normalize_source_operators(cls, value: list[str]) -> list[str]:
        normalized = [" ".join(str(item).split())[:100] for item in value]
        return list(dict.fromkeys(item for item in normalized if item))

    @field_validator(
        "bindingTime", "firstLoginAt", "lastUsedAt", "sourceCreatedAt"
    )
    @classmethod
    def business_timezone_required(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("business timestamps must contain a timezone")
        return value


class BuyerAccountSnapshotBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: Literal[
        "vendor_import",
        "legacy_feishu_migration",
        "environment_creation",
        "registration",
    ]
    snapshotKey: str = Field(min_length=8, max_length=128, pattern=SAFE_REF_RE)
    accounts: list[BuyerAccountSnapshotItem] = Field(max_length=500)

    @model_validator(mode="after")
    def unique_account_refs(self) -> "BuyerAccountSnapshotBody":
        refs = [item.accountRef for item in self.accounts]
        if len(refs) != len(set(refs)):
            raise ValueError("accountRef must be unique in one snapshot")
        return self


def safe_snapshot_items(body: BuyerAccountSnapshotBody) -> list[dict[str, object]]:
    """Return validated input; encryption happens inside the database service."""

    normalized: list[dict[str, object]] = []
    for item in body.accounts:
        payload = item.model_dump(mode="python")
        if item.credentials is None:
            payload.pop("credentials", None)
        else:
            payload["credentials"] = item.credentials.model_dump(mode="python")
        if "businessProfile" not in item.model_fields_set:
            payload.pop("businessProfile", None)
        else:
            payload["businessProfile"] = item.businessProfile.model_dump(mode="json")
        normalized.append(payload)
    return normalized


class BuyerAccountPreflightItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    accountRef: str = Field(min_length=8, max_length=128, pattern=SAFE_REF_RE)
    sourceOrderRef: str | None = Field(
        default=None, min_length=8, max_length=128, pattern=SAFE_REF_RE
    )


class BuyerAccountPreflightBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[BuyerAccountPreflightItem] = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def unique_references(self) -> "BuyerAccountPreflightBody":
        account_refs = [item.accountRef for item in self.items]
        if len(account_refs) != len(set(account_refs)):
            raise ValueError("accountRef must be unique in one preflight")
        order_refs = [item.sourceOrderRef for item in self.items if item.sourceOrderRef]
        if len(order_refs) != len(set(order_refs)):
            raise ValueError("sourceOrderRef must be unique in one preflight")
        return self
