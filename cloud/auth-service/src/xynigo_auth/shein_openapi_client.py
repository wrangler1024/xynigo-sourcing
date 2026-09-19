"""SHEIN open-platform gateway client: signing, token exchange, store info.

契约依据 20260916-17 生产真机实测（docs/20260917_需求_SHEIN开放平台店铺授权模块.md）：

已生产实证（观潮真店手动流程，可信任）：
- 店铺级签名：VALUE=`openKeyId&{timestamp}&{path}`，KEY=secretKey+RandomKey，
  HMAC-SHA256 → hex 小写 → base64，signature=RandomKey+base64(hex)；
  timestamp 为毫秒；头 x-lt-openKeyId/x-lt-timestamp/x-lt-signature。
- 解密：key=APP_Secret UTF-8 前 16 字节，IV=固定种子 ``space-station-de``，
  PKCS7 去填充，得 32 位十六进制串。

⚠️ UNVERIFIED（合成假设，联调首日必须用观潮真机响应校准，失败先对契约而非改实现）：
- 应用级签名：假定与店铺级同构，仅身份字段换成 appid（头 x-lt-appid）。
- `exchange_temp_token` 的响应结构：假定 ``{"code":"0","msg":...,"data":
  {"openKeyId":...,"secretKey":<hex AES 密文>}}``。
- tempToken 一次性、10 分钟有效；过期错误码假定为 code=33051002。
- `query_store_info` 的商家ID/店铺名字段名：假定 merchantId / storeName
  （代码同时兼容 snake_case 变体）。

应用凭证只从部署配置注入，不落库不落日志。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time
from typing import Any

import httpx
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

TOKEN_PATH = "/open-api/auth/get-by-token"
STORE_INFO_PATH = "/open-api/openapi-business-backend/query-store-info"

# ---- 财务 / 订单域（结算看板用）----
REPORT_ORDER_LIST_PATH = "/open-api/finance/report-order-list"
CHECK_ORDER_LIST_PATH = "/open-api/finance/get-check-order-list"
CHECK_ORDER_DETAIL_PATH = "/open-api/finance/get-check-order-detail"
ORDER_LIST_PATH = "/open-api/order/order-list"
ORDER_DETAIL_PATH = "/open-api/order/order-detail"
SITE_LIST_PATH = "/open-api/goods/query-site-list"

# 网关响应封装按接口族不同：换钥/店铺信息走 `data`，财务与订单域走 `info`
# （开放平台文档响应示例：{"code":"0","msg":"OK","info":{…},"bbl":{},"traceId":…}）。
# 两段都接受、info 优先；都缺时把实际键名带进错误信息，首次联调即可自诊断。
_ENVELOPE_KEYS = ("info", "data")

# 查询窗口上限（实测）：对账单按生成时间**必须严格小于 7 天**——文档写「不可超过
# 7 天」，但实测"恰好 7 天整"仍报 gsfs99401（20260917 真机：09-10 00:00:00 →
# 09-17 00:00:00 被拒），故取 7 天减 1 秒。订单列表同理取 48h 减 1 秒。
CHECK_ORDER_MAX_WINDOW_DAYS = 7
ORDER_LIST_MAX_WINDOW_HOURS = 48

# 单次批量上限（文档口径）
ORDER_DETAIL_MAX_BATCH = 30
CHECK_ORDER_LIST_PAGE_MAX = 30
ORDER_LIST_PAGE_MAX = 30
BZ_ORDER_NO_MAX = 100

TEMP_TOKEN_EXPIRED_CODE = "33051002"
_TIMEOUT = httpx.Timeout(15.0, connect=10.0)


class SheinOpenApiClientError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


# RandomKey 必须**恰好 5 位**（平台《API签名指南》明文「5位随机字符串」）。
# 平台按固定前 5 位切分签名来重建 KEY，长度不对就会切错前缀 →
# `openapi00001 签名错误:生成的签名不正确`。20260917 生产真机实测：
# 8 位随机串稳定失败、换 5 位立刻 HTTP 200 code=0。
# 这一条只能靠打真网关发现——自签自验的合成用例里长度永远自洽。
RANDOM_KEY_LENGTH = 5
_RANDOM_KEY_ALPHABET = "abcdefghijklmnopqrstuvwxyz0123456789"


def _random_key() -> str:
    return "".join(
        secrets.choice(_RANDOM_KEY_ALPHABET) for _ in range(RANDOM_KEY_LENGTH)
    )


def sign_headers(
    *,
    identity_header: str,
    identity: str,
    secret: str,
    path: str,
) -> dict[str, str]:
    """构造网关签名请求头（应用级与店铺级共用算法）。

    算法（以开放平台《API签名指南》为准，20260917 逐条核对）：
    VALUE = OpenKeyId & Timestamp & Path
    KEY   = SecretKey + RandomKey（RandomKey 恰好 5 位）
    Hex   = HMAC-SHA256(VALUE, KEY) 小写十六进制
    Signature = RandomKey + Base64(Hex)
    """
    timestamp = str(int(time.time() * 1000))
    random_key = _random_key()
    value = f"{identity}&{timestamp}&{path}"
    digest = hmac.new(
        (secret + random_key).encode("utf-8"), value.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    signature = random_key + base64.b64encode(digest.encode("ascii")).decode(
        "ascii"
    )
    return {
        identity_header: identity,
        "x-lt-timestamp": timestamp,
        "x-lt-signature": signature,
    }


def decrypt_secret_key(ciphertext: str, app_secret: str) -> str:
    """解密平台返回的 AES-128-CBC secretKey 密文。

    官方《店铺授权应用手册》Python 示例逐条核对（20260919）：
    密文为 **base64**（不是 hex）、AES-128-CBC、key=appSecret 前 16 字节
    （不足补零、超出截断）、IV=固定种子 "space-station-default-iv" 前 16
    字节、PKCS7 去填充、明文为 32 位十六进制串。
    """
    key = str(app_secret).encode("utf-8")[:16]
    key = key + bytes(16 - len(key))
    try:
        decryptor = Cipher(
            algorithms.AES(key), modes.CBC(b"space-station-de")
        ).decryptor()
        padded = decryptor.update(base64.b64decode(ciphertext)) + decryptor.finalize()
    except (ValueError, TypeError) as exc:
        raise SheinOpenApiClientError(
            "shein_secret_ciphertext_invalid", "店铺密钥密文无法解密"
        ) from exc
    pad = padded[-1]
    if not 1 <= pad <= 16 or padded[-pad:] != bytes([pad]) * pad:
        raise SheinOpenApiClientError(
            "shein_secret_padding_invalid", "店铺密钥填充无效"
        )
    secret = padded[:-pad].decode("utf-8", errors="strict")
    if len(secret) != 32:
        raise SheinOpenApiClientError(
            "shein_secret_length_invalid", "店铺密钥长度异常"
        )
    return secret


class SheinOpenApiClient:
    """同步客户端；transport 可注入供合成测试，生产直连网关。"""

    def __init__(
        self,
        *,
        gateway: str,
        app_id: str,
        app_secret: str,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.gateway = str(gateway or "").rstrip("/")
        self.app_id = str(app_id or "").strip()
        self.app_secret = str(app_secret or "")
        self._transport = transport

    @property
    def configured(self) -> bool:
        return bool(self.gateway and self.app_id and self.app_secret)

    def _unwrap(self, payload: dict[str, Any], *, path: str) -> dict[str, Any]:
        for key in _ENVELOPE_KEYS:
            section = payload.get(key)
            if isinstance(section, dict):
                return section
        raise SheinOpenApiClientError(
            "shein_gateway_data_invalid",
            f"{path} 响应缺少业务数据段（实际顶层键：{sorted(payload.keys())}）",
        )

    def _request(
        self, *, method: str, path: str, identity: str, secret: str,
        identity_header: str, body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        headers = sign_headers(
            identity_header=identity_header,
            identity=identity,
            secret=secret,
            path=path,
        )
        headers["Content-Type"] = "application/json"
        request = {"headers": headers}
        if params is not None:
            request["params"] = params
        if body is not None:
            # GET 端点（如对账单详情）签名仍按 path 计算，参数走 query。
            request["json"] = body
        try:
            with httpx.Client(
                base_url=self.gateway, timeout=_TIMEOUT, transport=self._transport,
            ) as client:
                response = client.request(method, path, **request)
        except httpx.HTTPError as exc:
            raise SheinOpenApiClientError(
                "shein_gateway_unreachable", "SHEIN 网关暂时无法访问"
            ) from exc
        if response.status_code != 200:
            raise SheinOpenApiClientError(
                "shein_gateway_http_error",
                f"SHEIN 网关返回 HTTP {response.status_code}",
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise SheinOpenApiClientError(
                "shein_gateway_payload_invalid", "SHEIN 网关响应不是 JSON"
            ) from exc
        if str(payload.get("code", "")) != "0":
            raise SheinOpenApiClientError(
                str(payload.get("code") or "shein_gateway_code_unknown"),
                str(payload.get("msg") or payload.get("message") or "SHEIN 网关业务错误"),
            )
        return self._unwrap(payload, path=path)

    def _post(
        self, *, path: str, identity: str, secret: str,
        identity_header: str, body: dict[str, Any],
    ) -> dict[str, Any]:
        return self._request(
            method="POST", path=path, identity=identity, secret=secret,
            identity_header=identity_header, body=body,
        )

    def _get(
        self, *, path: str, identity: str, secret: str,
        identity_header: str, params: dict[str, Any],
    ) -> dict[str, Any]:
        return self._request(
            method="GET", path=path, identity=identity, secret=secret,
            identity_header=identity_header, params=params,
        )

    # ---- 财务域 ----

    def query_report_orders(
        self, *, open_key_id: str, secret_key: str,
        page: int = 1, page_size: int = 30,
        report_order_no: str | None = None,
        report_status: int | None = None,
        completed_pay_date: str | None = None,
        completed_pay_time_start: str | None = None,
        completed_pay_time_end: str | None = None,
    ) -> dict[str, Any]:
        """报账单列表（已结算 reportStatus=2 / 即将付款 =1 / 异常 =3）。

        结算看板的「历史累计已结算」按 reportStatus=2 全量回填后聚合。
        文档明确本接口限流 20 次/秒（非订单类的 100）。
        """
        body: dict[str, Any] = {"page": int(page), "pageSize": int(page_size)}
        for key, value in (
            ("reportOrderNo", report_order_no),
            ("reportStatus", report_status),
            ("completedPayDate", completed_pay_date),
            ("completedPayTimeStart", completed_pay_time_start),
            ("completedPayTimeEnd", completed_pay_time_end),
        ):
            if value is not None and value != "":
                body[key] = value
        return self._post(
            path=REPORT_ORDER_LIST_PATH, identity=open_key_id,
            secret=secret_key, identity_header="x-lt-openKeyId", body=body,
        )

    def query_check_orders(
        self, *, open_key_id: str, secret_key: str,
        start_add_time: str, end_add_time: str,
        page: int = 1, page_size: int = 30,
        check_status: int | None = None,
        bz_order_nos: list[str] | None = None,
        report_order_nos: list[str] | None = None,
    ) -> dict[str, Any]:
        """对账单列表（待结算 / 下次打款）。

        窗口按**对账单生成时间**计算且必须 ≤7 天整，毫秒越界即报 gsfs99401
        ——分片时请把边界取到整秒（见 service 层的分片函数）。
        """
        body: dict[str, Any] = {
            "page": int(page), "pageSize": int(page_size),
            "startAddTime": start_add_time, "endAddTime": end_add_time,
        }
        if check_status is not None:
            body["checkStatus"] = int(check_status)
        if bz_order_nos:
            body["bzOrderNos"] = list(bz_order_nos)[:BZ_ORDER_NO_MAX]
        if report_order_nos:
            body["reportOrderNos"] = list(report_order_nos)
        return self._post(
            path=CHECK_ORDER_LIST_PATH, identity=open_key_id,
            secret=secret_key, identity_header="x-lt-openKeyId", body=body,
        )

    def get_check_order_detail(
        self, *, open_key_id: str, secret_key: str, check_order_no: str
    ) -> dict[str, Any]:
        """对账单详情：返回 itemList 逐 SKU 费用拆分（佣金/服务费/税费等）。

        有了它，逐单精确对账不必再依赖无权限的 report-sales-detail。
        注意本接口是 **GET**，对账单单号走 query 参数。
        """
        return self._get(
            path=CHECK_ORDER_DETAIL_PATH, identity=open_key_id, secret=secret_key,
            identity_header="x-lt-openKeyId",
            params={"checkOrderNo": str(check_order_no or "").strip()},
        )

    # ---- 订单域 ----

    def query_orders(
        self, *, open_key_id: str, secret_key: str,
        query_type: int, start_time: str, end_time: str,
        page: int = 1, page_size: int = 30,
        order_status: int | None = None,
    ) -> dict[str, Any]:
        """订单列表：只返回单号、状态、时间（金额需再调订单详情）。

        queryType 1=按下单时间 / 2=按更新时间；窗口 ≤48h（UTC+8）。
        在途口径依赖本地状态台账——按更新时间滚动增量，全量回溯见 service 层。
        """
        body: dict[str, Any] = {
            "queryType": int(query_type),
            "startTime": start_time, "endTime": end_time,
            "page": int(page), "pageSize": int(page_size),
        }
        if order_status is not None:
            body["orderStatus"] = int(order_status)
        return self._post(
            path=ORDER_LIST_PATH, identity=open_key_id,
            secret=secret_key, identity_header="x-lt-openKeyId", body=body,
        )

    def query_order_details(
        self, *, open_key_id: str, secret_key: str, order_nos: list[str]
    ) -> dict[str, Any]:
        """订单详情（含 estimatedGrossIncome 预计收入）。单次 ≤30 单号。"""
        batch = [str(no) for no in order_nos][:ORDER_DETAIL_MAX_BATCH]
        return self._post(
            path=ORDER_DETAIL_PATH, identity=open_key_id,
            secret=secret_key, identity_header="x-lt-openKeyId",
            body={"orderNoList": batch},
        )

    def query_site_list(
        self, *, open_key_id: str, secret_key: str
    ) -> dict[str, Any]:
        """店铺站点与站点币种——多币种折算时币种口径的权威来源。

        **POST**（实测：按 GET 调会报 openapi00007 请求头参数异常）。
        响应为 info.data[]，每项含 main_site / sub_site_list[{currency, site_abbr, …}]。
        """
        return self._post(
            path=SITE_LIST_PATH, identity=open_key_id, secret=secret_key,
            identity_header="x-lt-openKeyId", body={},
        )

    def exchange_temp_token(self, temp_token: str) -> tuple[str, str]:
        """tempToken → (openKeyId, secretKey 明文)。tempToken 只在本次调用使用。"""
        data = self._post(
            path=TOKEN_PATH,
            identity=self.app_id,
            secret=self.app_secret,
            identity_header="x-lt-appid",
            body={"tempToken": str(temp_token or "").strip()},
        )
        open_key_id = str(data.get("openKeyId") or "").strip()
        ciphertext = str(data.get("secretKey") or "").strip()
        if not open_key_id or not ciphertext:
            raise SheinOpenApiClientError(
                "shein_token_exchange_incomplete", "换取店铺密钥返回不完整"
            )
        return open_key_id, decrypt_secret_key(ciphertext, self.app_secret)

    def query_store_info(
        self, *, open_key_id: str, secret_key: str
    ) -> dict[str, Any]:
        return self._post(
            path=STORE_INFO_PATH,
            identity=open_key_id,
            secret=secret_key,
            identity_header="x-lt-openKeyId",
            body={},
        )
