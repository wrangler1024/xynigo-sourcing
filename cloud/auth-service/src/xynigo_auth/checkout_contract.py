"""采购下单闭环 P1 请求契约：只接受安全资源引用，不接受任何凭证。"""

from __future__ import annotations

import re
import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


SAFE_KEY_RE = r"^[A-Za-z0-9._:-]+$"
PLATFORM_ORDER_RE = re.compile(r"^[A-Z0-9_-]{6,200}$")
TRACKING_RE = re.compile(r"^[A-Za-z0-9._/-]{3,200}$")


def _normalized_text(value: str) -> str:
    return " ".join(value.split())


class CheckoutResourceBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    hubEnvironmentRef: str = Field(min_length=1, max_length=128, pattern=SAFE_KEY_RE)
    hubEnvironmentName: str = Field(min_length=1, max_length=255)
    buyerAccountId: uuid.UUID
    site: Literal["US", "MX"]

    @field_validator("hubEnvironmentName")
    @classmethod
    def safe_labels_only(cls, value: str) -> str:
        normalized = _normalized_text(value)
        if not normalized:
            raise ValueError("resource label must be display-safe")
        return normalized


class CheckoutAllocationBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    purchaseOrderLineId: uuid.UUID
    quantity: int = Field(ge=1, le=100_000)


class CheckoutPlanMixin(BaseModel):
    model_config = ConfigDict(extra="forbid")

    resource: CheckoutResourceBody | None = None
    note: str = Field(default="", max_length=1000)
    lines: list[CheckoutAllocationBody] = Field(min_length=1, max_length=200)

    @field_validator("note")
    @classmethod
    def normalize_note(cls, value: str) -> str:
        return _normalized_text(value)

    @model_validator(mode="after")
    def unique_lines(self) -> "CheckoutPlanMixin":
        line_ids = [line.purchaseOrderLineId for line in self.lines]
        if len(line_ids) != len(set(line_ids)):
            raise ValueError("a purchase line can appear only once in an attempt")
        return self


class CheckoutAttemptCreateBody(CheckoutPlanMixin):
    idempotencyKey: str = Field(min_length=8, max_length=128, pattern=SAFE_KEY_RE)
    expectedExecutionRevision: int = Field(ge=0)


class CheckoutAttemptReviseBody(CheckoutPlanMixin):
    expectedVersion: int = Field(ge=1)
    expectedExecutionRevision: int = Field(ge=0)


class CheckoutAttemptBeginBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expectedVersion: int = Field(ge=1)


class CheckoutAttemptAbandonBody(CheckoutAttemptBeginBody):
    reason: str = Field(min_length=2, max_length=500)

    @field_validator("reason")
    @classmethod
    def normalize_reason(cls, value: str) -> str:
        return _normalized_text(value)


class CheckoutPaymentResultBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expectedVersion: int = Field(ge=1)
    outcome: Literal["paid", "failed", "uncertain"]
    environmentLoggedIn: bool
    platform: Literal["SHEIN"] = "SHEIN"
    platformOrderNo: str = Field(default="", max_length=200)
    actualAmount: Decimal | None = Field(
        default=None,
        ge=0,
        max_digits=18,
        decimal_places=2,
    )
    currency: str = Field(default="", max_length=12)
    discountAmount: Decimal | None = Field(
        default=None,
        ge=0,
        max_digits=18,
        decimal_places=2,
    )
    couponSummary: str = Field(default="", max_length=500)
    paidAt: datetime | None = None
    reason: str = Field(default="", max_length=500)

    @field_validator("actualAmount", "discountAmount")
    @classmethod
    def finite_amounts_only(cls, value: Decimal | None) -> Decimal | None:
        if value is not None and not value.is_finite():
            raise ValueError("amount must be finite")
        return value

    @field_validator("platformOrderNo")
    @classmethod
    def normalize_platform_order(cls, value: str) -> str:
        normalized = value.strip().upper()
        if normalized and not PLATFORM_ORDER_RE.fullmatch(normalized):
            raise ValueError("invalid platform order number")
        return normalized

    @field_validator("currency")
    @classmethod
    def normalize_currency(cls, value: str) -> str:
        return value.strip().upper()

    @field_validator("couponSummary", "reason")
    @classmethod
    def normalize_text(cls, value: str) -> str:
        return _normalized_text(value)

    @field_validator("paidAt")
    @classmethod
    def timezone_required(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("paidAt must contain a timezone")
        return value

    @model_validator(mode="after")
    def validate_outcome_fields(self) -> "CheckoutPaymentResultBody":
        if self.outcome == "paid":
            if not self.platformOrderNo or self.actualAmount is None or not self.currency:
                raise ValueError("paid result requires order number, amount and currency")
            if self.paidAt is None:
                raise ValueError("paid result requires paidAt")
        elif len(self.reason) < 2:
            raise ValueError("failed or uncertain result requires a reason")
        return self


class CheckoutCleanupResultBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expectedVersion: int = Field(ge=1)
    environmentResult: Literal["deleted", "not_created", "delete_failed"]
    buyerResult: Literal["reusable", "manual_review"]
    reason: str = Field(min_length=2, max_length=500)

    @field_validator("reason")
    @classmethod
    def normalize_reason(cls, value: str) -> str:
        return _normalized_text(value)


class ShipmentUpsertBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    shipmentKey: str = Field(min_length=3, max_length=128, pattern=SAFE_KEY_RE)
    expectedVersion: int = Field(ge=0)
    packageNo: str = Field(default="", max_length=200)
    carrierCode: str = Field(default="", max_length=64, pattern=r"^[A-Za-z0-9._:-]*$")
    carrierName: str = Field(min_length=1, max_length=128)
    trackingNo: str = Field(min_length=3, max_length=200)
    status: Literal["pending_pickup", "in_transit", "delivered", "exception"]
    shippedAt: datetime | None = None
    deliveredAt: datetime | None = None

    @field_validator("packageNo", "carrierName")
    @classmethod
    def normalize_text(cls, value: str) -> str:
        return _normalized_text(value)

    @field_validator("carrierCode")
    @classmethod
    def normalize_carrier_code(cls, value: str) -> str:
        return value.strip().upper()

    @field_validator("trackingNo")
    @classmethod
    def validate_tracking_no(cls, value: str) -> str:
        normalized = value.strip().upper()
        if not TRACKING_RE.fullmatch(normalized):
            raise ValueError("invalid tracking number")
        return normalized

    @field_validator("shippedAt", "deliveredAt")
    @classmethod
    def timezone_required(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("shipment time must contain a timezone")
        return value

    @model_validator(mode="after")
    def validate_delivery_time(self) -> "ShipmentUpsertBody":
        if self.status == "delivered" and self.deliveredAt is None:
            raise ValueError("delivered shipment requires deliveredAt")
        if (
            self.shippedAt is not None
            and self.deliveredAt is not None
            and self.deliveredAt < self.shippedAt
        ):
            raise ValueError("deliveredAt cannot be earlier than shippedAt")
        return self


def plan_payload(body: CheckoutPlanMixin) -> dict[str, Any]:
    """输出领域层需要的纯 Python 安全载荷。"""

    return body.model_dump(mode="python")
