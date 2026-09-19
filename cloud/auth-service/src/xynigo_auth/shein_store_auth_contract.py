"""Request/response contracts for SHEIN store authorization."""

from __future__ import annotations

from pydantic import BaseModel, Field

STORE_MODES = ("self", "semi")


class SheinAuthLinkBody(BaseModel):
    mode: str = Field(default="self", pattern="^(self|semi)$")
    # 重新授权时带目标店 id：回调优先更新该行，避免店铺信息接口失败的
    # 场景把同店拆成两行（评审必须改 #1）。
    storeId: str = Field(default="", max_length=64)


class SheinAuthCallbackBody(BaseModel):
    """state 即一次性能力凭证：回调端点不要求 xynigo 登录态。

    state 允许为空：平台回跳可能剥掉 redirectUrl 自带 query，此时由
    服务端按最新未消费链接回退认领（见 _claim_link）。
    """

    tempToken: str = Field(min_length=8, max_length=256)
    state: str = Field(default="", max_length=128)


class SheinStoreRenameBody(BaseModel):
    name: str = Field(min_length=1, max_length=128)


class SheinStoreListQuery(BaseModel):
    mode: str = ""
    keyword: str = ""
    page: int = Field(default=1, ge=1)
    pageSize: int = Field(default=10, ge=1, le=100)
