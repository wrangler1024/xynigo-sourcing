"""店小秘扩展提交的采购草稿契约（Pydantic），相当于 Java 的 DTO + 校验。

校验通过后才允许写入 Postgres。extra=forbid 表示多传未知字段会直接拒绝。
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import datetime
from typing import Any, Literal
from urllib.parse import parse_qs, urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


PACKAGE_ID_RE = re.compile(r"^XMWU[A-Z0-9_-]+$")
PLATFORM_ORDER_RE = re.compile(r"^G(?:SH|SU)[A-Z0-9_-]+$")
TRUSTED_IMAGE_HOST_RE = re.compile(r"(?:^|\.)ltwebstatic\.com$", re.IGNORECASE)
TRUSTED_SHEIN_HOST_RE = re.compile(r"(?:^|\.)shein\.com(?:\.mx)?$", re.IGNORECASE)
STORE_ASSIGNMENT_RE = re.compile(
    r"^(.+)\s*-\s*([^-（）()]+?)\s*[（(][^（）()]*[）)]\s*[$¥￥]?\s*$"
)


def _blank_to_none(value: Any) -> Any:
    if isinstance(value, str) and not value.strip():
        return None
    return value


def parse_store_assignment(store_name: str) -> tuple[str, str]:
    """从店小秘店铺展示名拆出「店铺名」和「运营姓名」。

    期望格式：店铺名-运营姓名（组别）。对不上则整串当店铺名，运营名为空。
    """
    normalized = " ".join(str(store_name or "").split())
    matched = STORE_ASSIGNMENT_RE.fullmatch(normalized)
    if matched is None:
        return normalized, ""
    return " ".join(matched.group(1).split()), " ".join(matched.group(2).split())


class EstimatedMetrics(BaseModel):
    """扩展带过来的预估销售额/成本/利润；禁止 NaN/Inf。"""

    model_config = ConfigDict(extra="forbid")

    ok: bool
    reason: str | None = Field(default=None, max_length=300)
    currency: str | None = Field(default=None, max_length=12)
    salesAmount: float | None = None
    guideTotal: float | None = None
    estimatedTopUpAmount: float | None = None
    estimatedCost: float | None = None
    estimatedProfit: float | None = None
    profitMargin: float | None = None
    roi: float | None = None
    minimumApplied: bool | None = None
    costBasis: str | None = Field(default=None, max_length=100)

    @field_validator(
        "salesAmount",
        "guideTotal",
        "estimatedTopUpAmount",
        "estimatedCost",
        "estimatedProfit",
        "profitMargin",
        "roi",
    )
    @classmethod
    def finite_numbers_only(cls, value: float | None) -> float | None:
        if value is not None and not math.isfinite(value):
            raise ValueError("estimated metric must be finite")
        return value


class PurchaseDraftLine(BaseModel):
    """草稿里的一条 SKU 明细。正式提交还要校验 SHEIN 链接与 goods_id/skucode 一致。"""

    model_config = ConfigDict(extra="forbid")

    lineNo: int = Field(ge=1, le=200)
    sellerSku: str = Field(default="", max_length=300)
    variant: str = Field(default="", max_length=500)
    productImageUrl: str = Field(default="", max_length=5000)
    mainSpec: str = Field(default="", max_length=500)
    subSpec: str = Field(default="", max_length=500)
    originalPrice: float | None = Field(default=None, ge=0)
    couponType: str = Field(default="", max_length=100)
    guidePrice: float | None = Field(default=None, ge=0)
    purchaseCurrency: str = Field(default="", max_length=12)
    salesQty: int = Field(ge=1, le=100000)
    purchaseQty: int | None = Field(default=None, ge=1, le=100000)
    source: str = Field(default="", max_length=100)
    purchaseLink: str = Field(default="", max_length=5000)
    goodsId: str = Field(default="", max_length=64)
    skuCode: str = Field(default="", max_length=300)
    mainAttr: str = Field(default="", max_length=300)
    mallCode: str = Field(default="", max_length=100)

    @field_validator("originalPrice", "guidePrice", "purchaseQty", mode="before")
    @classmethod
    def accept_browser_blank_values(cls, value: Any) -> Any:
        return _blank_to_none(value)

    @field_validator("originalPrice", "guidePrice")
    @classmethod
    def finite_prices_only(cls, value: float | None) -> float | None:
        if value is not None and not math.isfinite(value):
            raise ValueError("price must be finite")
        return value

    @field_validator("purchaseCurrency")
    @classmethod
    def normalize_currency(cls, value: str) -> str:
        return value.strip().upper()

    @field_validator("productImageUrl")
    @classmethod
    def validate_product_image_url(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            return ""
        parsed = urlsplit(normalized)
        hostname = (parsed.hostname or "").rstrip(".")
        if parsed.scheme != "https" or not TRUSTED_IMAGE_HOST_RE.search(hostname):
            raise ValueError("product image must use a trusted SHEIN HTTPS host")
        return normalized


class PurchaseDraft(BaseModel):
    """店小秘扩展提交的整单草稿。schemaVersion=2 起要求能识别店铺归属。"""

    model_config = ConfigDict(extra="forbid")

    schemaVersion: Literal[1, 2]
    mode: Literal["local-dev-mock", "xynigo-extension"]
    orderKey: str = Field(min_length=1, max_length=800)
    packageId: str = Field(min_length=1, max_length=200)
    platformOrderNo: str = Field(min_length=1, max_length=200)
    storeName: str = Field(min_length=1, max_length=300)
    storeBaseName: str = Field(default="", max_length=300)
    operatorName: str = Field(default="", max_length=100)
    site: str = Field(default="", max_length=20)
    salesCurrency: str = Field(default="", max_length=12)
    salesAmount: float | None = Field(default=None, ge=0)
    dianxiaomiOrderTime: str = Field(default="", max_length=19)
    recipientName: str = Field(default="", max_length=300)
    recipientPhone: str = Field(default="", max_length=100)
    addressLine1: str = Field(default="", max_length=1000)
    addressLine2: str = Field(default="", max_length=1000)
    city: str = Field(default="", max_length=300)
    stateProvince: str = Field(default="", max_length=300)
    postalCode: str = Field(default="", max_length=100)
    items: list[PurchaseDraftLine] = Field(default_factory=list, max_length=200)
    guideTotalsByCurrency: dict[str, float] = Field(default_factory=dict)
    estimatedMetrics: EstimatedMetrics | None = None
    remarkText: Literal[""]
    remarkStatus: Literal["not-generated"]
    purchaseStatus: Literal["draft-local"]
    submissionStatus: Literal["draft"]
    createdAt: datetime
    updatedAt: datetime

    @field_validator("salesAmount", mode="before")
    @classmethod
    def accept_blank_sales_amount(cls, value: Any) -> Any:
        return _blank_to_none(value)

    @field_validator("salesAmount")
    @classmethod
    def finite_sales_amount(cls, value: float | None) -> float | None:
        if value is not None and not math.isfinite(value):
            raise ValueError("sales amount must be finite")
        return value

    @field_validator("packageId")
    @classmethod
    def normalize_package_id(cls, value: str) -> str:
        normalized = value.strip().upper()
        if not PACKAGE_ID_RE.fullmatch(normalized):
            raise ValueError("invalid package id")
        return normalized

    @field_validator("platformOrderNo")
    @classmethod
    def normalize_platform_order(cls, value: str) -> str:
        normalized = value.strip().upper()
        if not PLATFORM_ORDER_RE.fullmatch(normalized):
            raise ValueError("invalid platform order number")
        return normalized

    @field_validator("storeName", "storeBaseName", "operatorName")
    @classmethod
    def normalize_store_fields(cls, value: str) -> str:
        normalized = " ".join(value.split())
        return normalized

    @field_validator("salesCurrency")
    @classmethod
    def normalize_sales_currency(cls, value: str) -> str:
        return value.strip().upper()

    @field_validator("site")
    @classmethod
    def normalize_site(cls, value: str) -> str:
        normalized = value.strip().upper()
        if normalized and normalized not in ("US", "MX"):
            raise ValueError("site currently supports US or MX")
        return normalized

    @field_validator("dianxiaomiOrderTime")
    @classmethod
    def validate_dianxiaomi_order_time(cls, value: str) -> str:
        normalized = value.strip()
        if normalized and not re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", normalized):
            raise ValueError("invalid Dianxiaomi order time")
        return normalized

    @field_validator(
        "recipientName",
        "recipientPhone",
        "addressLine1",
        "addressLine2",
        "city",
        "stateProvince",
        "postalCode",
    )
    @classmethod
    def normalize_recipient_text(cls, value: str) -> str:
        return value.strip()

    @field_validator("guideTotalsByCurrency")
    @classmethod
    def validate_guide_totals(cls, value: dict[str, float]) -> dict[str, float]:
        normalized: dict[str, float] = {}
        for currency, amount in value.items():
            key = str(currency).strip().upper()
            number = float(amount)
            if not key or len(key) > 12 or not math.isfinite(number) or number < 0:
                raise ValueError("invalid guide total")
            normalized[key] = number
        return normalized

    @field_validator("createdAt", "updatedAt")
    @classmethod
    def timezone_required(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("client time must contain a timezone")
        return value

    @model_validator(mode="after")
    def validate_cross_fields(self) -> "PurchaseDraft":
        if not self.site:
            self.site = {"USD": "US", "MXN": "MX"}.get(self.salesCurrency, "")
        if self.updatedAt < self.createdAt:
            raise ValueError("updatedAt cannot be earlier than createdAt")
        if [item.lineNo for item in self.items] != list(range(1, len(self.items) + 1)):
            raise ValueError("lineNo must be continuous from 1")
        if self.orderKey != create_order_key(self.storeName, self.platformOrderNo, self.packageId):
            raise ValueError("orderKey does not match the order identity")
        parsed_store, parsed_operator = parse_store_assignment(self.storeName)
        if self.storeBaseName and self.storeBaseName != parsed_store:
            raise ValueError("storeBaseName does not match storeName")
        if self.operatorName and self.operatorName != parsed_operator:
            raise ValueError("operatorName does not match storeName")
        return self


def create_order_key(store_name: str, platform_order_no: str, package_id: str) -> str:
    """租户内采购单业务键：店铺|平台单号|包裹号，须与客户端 orderKey 一致。"""
    store = " ".join(str(store_name).split()).lower()
    order_no = " ".join(str(platform_order_no).split()).upper()
    package = " ".join(str(package_id).split()).upper()
    return "|".join((store, order_no, package))


def canonical_draft_dict(draft: PurchaseDraft, *, include_client_times: bool = True) -> dict[str, Any]:
    """把草稿打成稳定 JSON 字典，用于落库和算哈希。v1 不含店铺归属字段。"""
    data = draft.model_dump(mode="json")
    if draft.schemaVersion == 1:
        data.pop("storeBaseName", None)
        data.pop("operatorName", None)
    if not include_client_times:
        data.pop("createdAt", None)
        data.pop("updatedAt", None)
    return data


def canonical_json(value: Any) -> str:
    """键排序、无空格的 JSON，保证同一内容哈希始终相同。"""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def draft_content_hash(draft: PurchaseDraft) -> str:
    """整单内容指纹（不含客户端时间）。相同则视为未改，避免无意义写库。"""
    raw = canonical_json(canonical_draft_dict(draft, include_client_times=False)).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def line_content_hash(line: PurchaseDraftLine) -> str:
    """单行明细指纹，用来判断这一行是否真的变了。"""
    return hashlib.sha256(canonical_json(line.model_dump(mode="json")).encode("utf-8")).hexdigest()


def line_key(order_key: str, line_no: int) -> str:
    """明细稳定键：订单键 + 行号。"""
    return f"{order_key}|{line_no}"


def validate_formal_submit(draft: PurchaseDraft) -> None:
    """正式提交比存草稿更严：收件人、SHEIN 链接、指导价、采购数量=销售数量。"""
    if not draft.items:
        raise ValueError("采购单至少需要一条采购明细")
    if draft.schemaVersion >= 2 and (not draft.storeBaseName or not draft.operatorName):
        raise ValueError("店铺归属无法识别，请确认店铺名格式为“店铺名-运营姓名（组别）”")
    required_recipient = {
        "收件人姓名": draft.recipientName,
        "收件人电话": draft.recipientPhone,
        "收货地址": draft.addressLine1,
        "城市": draft.city,
        "州/省": draft.stateProvince,
        "邮编": draft.postalCode,
    }
    missing = [label for label, value in required_recipient.items() if not value]
    if missing:
        raise ValueError("正式提交缺少" + "、".join(missing))
    for item in draft.items:
        prefix = f"第 {item.lineNo} 条采购明细"
        if not item.purchaseLink or not item.goodsId or not item.skuCode:
            raise ValueError(prefix + "缺少精确采购链接、goods_id 或 skucode")
        parsed = urlsplit(item.purchaseLink)
        hostname = (parsed.hostname or "").rstrip(".")
        if parsed.scheme != "https" or not TRUSTED_SHEIN_HOST_RE.search(hostname):
            raise ValueError(prefix + "必须使用 SHEIN HTTPS 采购链接")
        query = parse_qs(parsed.query, keep_blank_values=True)
        query_goods_ids = query.get("goods_id") or []
        query_sku_codes = query.get("skucode") or []
        if query_goods_ids != [item.goodsId] or query_sku_codes != [item.skuCode]:
            raise ValueError(prefix + "链接与 goods_id/skucode 不一致")
        if item.guidePrice is None or item.guidePrice <= 0:
            raise ValueError(prefix + "指导价必须大于 0")
        if not item.purchaseCurrency:
            raise ValueError(prefix + "缺少采购币种")
        if item.purchaseQty is None or item.purchaseQty != item.salesQty:
            raise ValueError(prefix + "采购数量必须等于销售数量")
