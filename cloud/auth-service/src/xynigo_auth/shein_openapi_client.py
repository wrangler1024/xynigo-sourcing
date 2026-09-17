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
    """解密平台返回的 AES-128-CBC secretKey 密文。"""
    key = str(app_secret).encode("utf-8")[:16]
    if len(key) != 16:
        raise SheinOpenApiClientError(
            "shein_app_secret_invalid", "应用密钥长度不足 16 字节"
        )
    try:
        decryptor = Cipher(
            algorithms.AES(key), modes.CBC(b"space-station-de")
        ).decryptor()
        padded = decryptor.update(bytes.fromhex(ciphertext)) + decryptor.finalize()
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

    def _post(
        self, *, path: str, identity: str, secret: str,
        identity_header: str, body: dict[str, Any],
    ) -> dict[str, Any]:
        headers = sign_headers(
            identity_header=identity_header,
            identity=identity,
            secret=secret,
            path=path,
        )
        headers["Content-Type"] = "application/json"
        try:
            with httpx.Client(
                base_url=self.gateway, timeout=_TIMEOUT, transport=self._transport,
            ) as client:
                response = client.post(path, json=body, headers=headers)
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
        data = payload.get("data")
        if not isinstance(data, dict):
            raise SheinOpenApiClientError(
                "shein_gateway_data_invalid", "SHEIN 网关返回数据缺失"
            )
        return data

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
