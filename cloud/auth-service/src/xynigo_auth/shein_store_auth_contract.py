"""Request/response contracts for SHEIN store authorization."""

from __future__ import annotations

from pydantic import BaseModel, Field

STORE_MODES = ("self", "semi")


class SheinAuthLinkBody(BaseModel):
    mode: str = Field(default="self", pattern="^(self|semi)$")


class SheinAuthCallbackBody(BaseModel):
    """state 即一次性能力凭证：回调端点不要求 xynigo 登录态。"""

    tempToken: str = Field(min_length=8, max_length=256)
    state: str = Field(min_length=16, max_length=128)


class SheinStoreRenameBody(BaseModel):
    name: str = Field(min_length=1, max_length=128)


class SheinStoreListQuery(BaseModel):
    mode: str = ""
    keyword: str = ""
    page: int = Field(default=1, ge=1)
    pageSize: int = Field(default=10, ge=1, le=100)
