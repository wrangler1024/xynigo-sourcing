# -*- coding: utf-8 -*-
"""SHEIN 网关签名契约：算法黄金样例 + RandomKey 长度。

这个文件的存在理由是一次真实事故（20260917 生产真机）：

原实现用 `secrets.token_hex(4)` 生成 RandomKey，即 **8 位**；而平台
《API签名指南》要求 RandomKey **恰好 5 位**，并且按固定前 5 位切分签名来
重建 KEY。长度不对 → 前缀切错 → 全部接口报 `openapi00001 签名错误`。

**为什么原测试没能拦住**：既有用例是"自己签、自己验"，长度在自洽的闭环里
永远成立；只有打真网关才暴露。所以这里补两类互不依赖的断言：

1. 黄金样例——用平台文档公布的输入与输出对齐（钉住算法本身，不依赖我方生成器）；
2. 长度断言——钉住 RandomKey 恰好 5 位（这是算法的契约组成部分，不是随机细节）。

参考：《SHEIN开放平台API签名指南》
https://open.sheincorp.com/zh/documents/system/passwdrule
"""
from __future__ import annotations

import base64
import hashlib
import hmac

from xynigo_auth.shein_openapi_client import (
    RANDOM_KEY_LENGTH,
    sign_headers,
)

# ---- 平台文档公布的黄金样例（逐字取自《API签名指南》步骤 2–6 的示例值）----
DOC_OPEN_KEY_ID = "B96C15416C9240DF96BAA0BC9B367C6D"
DOC_SECRET_KEY = "6BEC9C4B668B4B14B17EEF106BB98AE5"
DOC_PATH = "/open-api/order/purchase-order-info"
DOC_TIMESTAMP = "1740709414000"
DOC_RANDOM_KEY = "test1"
DOC_VALUE = (
    "B96C15416C9240DF96BAA0BC9B367C6D&1740709414000"
    "&/open-api/order/purchase-order-info"
)
DOC_HEX = "d6ca2c789f5307de567f77717fcf098b114aeb415534166e61d0d192ba95acca"
DOC_BASE64 = (
    "ZDZjYTJjNzg5ZjUzMDdkZTU2N2Y3NzcxN2ZjZjA5OGIxMTRhZWI0MTU1MzQx"
    "NjZlNjFkMGQxOTJiYTk1YWNjYQ=="
)
DOC_SIGNATURE = "test1" + DOC_BASE64


def test_documented_example_signature_is_reproduced():
    """用文档样例值复算：VALUE 拼装 → HMAC-SHA256 → hex 小写 → base64 → 前缀 RandomKey。

    这条不依赖我方随机串生成器，所以即使生成器写错也能独立验证算法。
    """
    digest = hmac.new(
        (DOC_SECRET_KEY + DOC_RANDOM_KEY).encode("utf-8"),
        DOC_VALUE.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    assert digest == DOC_HEX, "HMAC-SHA256 → 小写十六进制 与文档不一致"
    assert base64.b64encode(digest.encode("ascii")).decode() == DOC_BASE64
    assert DOC_RANDOM_KEY + base64.b64encode(
        digest.encode("ascii")).decode() == DOC_SIGNATURE


def test_sign_headers_produces_documented_algorithm_shape():
    """sign_headers 产出的签名必须能被同一算法验回——且前缀恰为 RandomKey。

    签名格式是 `RandomKey + base64`，平台按固定长度取前缀，所以前缀长度
    是契约的一部分，不能当成"随便一个短串"。
    """
    identity = "A" * 32
    secret = "B" * 32
    path = "/open-api/order/order-detail"
    headers = sign_headers(
        identity_header="x-lt-openKeyId", identity=identity,
        secret=secret, path=path,
    )
    assert set(headers) == {"x-lt-openKeyId", "x-lt-timestamp",
                            "x-lt-signature"}
    assert headers["x-lt-openKeyId"] == identity
    assert headers["x-lt-timestamp"].isdigit()
    assert len(headers["x-lt-timestamp"]) == 13, "时间戳应为毫秒"

    signature = headers["x-lt-signature"]
    random_key, encoded = signature[:RANDOM_KEY_LENGTH], signature[RANDOM_KEY_LENGTH:]
    expected_hex = hmac.new(
        (secret + random_key).encode("utf-8"),
        f"{identity}&{headers['x-lt-timestamp']}&{path}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    assert base64.b64encode(expected_hex.encode("ascii")).decode() == encoded


def test_random_key_is_exactly_five_characters():
    """回归位：RandomKey 长度错（曾是 8 位）会让真机全接口 401。

    只断言长度与字符集，不断言具体值。
    """
    assert RANDOM_KEY_LENGTH == 5, (
        "平台《API签名指南》要求 RandomKey 恰好 5 位；改这个值前先确认"
        "文档未变更，否则真机签名会全部失效"
    )
    seen: set[str] = set()
    for _ in range(50):
        signature = sign_headers(
            identity_header="x-lt-openKeyId", identity="A" * 32,
            secret="B" * 32, path="/p",
        )["x-lt-signature"]
        prefix = signature[:5]
        assert len(prefix) == 5
        assert prefix.isalnum(), f"RandomKey 应为字母数字：{prefix!r}"
        seen.add(prefix)
    assert len(seen) > 1, "RandomKey 看起来是固定值，应为随机"


def test_application_level_identity_header_is_respected():
    """换钥接口用 appid 作为身份头，算法与店铺级同构。"""
    headers = sign_headers(
        identity_header="x-lt-appid", identity="APPID0001",
        secret="S" * 32, path="/open-api/auth/get-by-token",
    )
    assert headers["x-lt-appid"] == "APPID0001"
    assert "x-lt-openKeyId" not in headers
