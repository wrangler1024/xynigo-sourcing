"""登录安全小工具：随机令牌、落库哈希、飞书 OAuth PKCE。

数据库只存 SHA-256 摘要，不存原始 session / poll token。
"""

from __future__ import annotations

import base64
import hashlib
import secrets


def random_url_token(byte_length: int = 32) -> str:
    """生成可放进 URL 的高熵随机串（发给浏览器或本地执行器的那份明文）。"""
    return secrets.token_urlsafe(byte_length)


def hash_token(value: str) -> str:
    """把令牌变成固定长度摘要再写入 Postgres。"""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def pkce_challenge(verifier: str) -> str:
    """OAuth PKCE：用 code_verifier 算出发给飞书的 code_challenge。"""
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
