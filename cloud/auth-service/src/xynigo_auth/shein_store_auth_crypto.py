"""Authenticated encryption for SHEIN store secret keys."""

from __future__ import annotations

import json
from typing import Any

from cryptography.fernet import Fernet, InvalidToken


class SheinStoreAuthCryptoError(RuntimeError):
    """Stable error without embedding encrypted or plaintext secrets."""


class SheinStoreSecretCipher:
    """Encrypt store secret-key envelopes with a deployment-owned Fernet key.

    与 BuyerCredentialCipher 同源（复用部署密钥），payload 只装 secretKey
    一个字段；密文前缀区分版本，未来轮换密钥时可按前缀识别。
    """

    PREFIX = "v1:"

    def __init__(self, key: str) -> None:
        normalized = str(key or "").strip().encode("ascii", errors="strict")
        if not normalized:
            raise SheinStoreAuthCryptoError("shein_store_key_unavailable")
        try:
            self._fernet = Fernet(normalized)
        except (TypeError, ValueError) as exc:
            raise SheinStoreAuthCryptoError("shein_store_key_invalid") from exc

    def encrypt_secret(self, secret_key: str) -> str:
        value = str(secret_key or "").strip()
        if not value:
            raise SheinStoreAuthCryptoError("shein_store_secret_blank")
        try:
            raw = json.dumps({"secretKey": value}).encode("utf-8")
            token = self._fernet.encrypt(raw).decode("ascii")
        except Exception as exc:
            raise SheinStoreAuthCryptoError("shein_store_encrypt_failed") from exc
        return self.PREFIX + token

    def decrypt_secret(self, ciphertext: str | None) -> str:
        if not ciphertext:
            raise SheinStoreAuthCryptoError("shein_store_secret_missing")
        value = str(ciphertext)
        if not value.startswith(self.PREFIX):
            raise SheinStoreAuthCryptoError("shein_store_ciphertext_version")
        try:
            raw = self._fernet.decrypt(
                value[len(self.PREFIX):].encode("ascii")
            )
            payload: dict[str, Any] = json.loads(raw.decode("utf-8"))
        except (InvalidToken, UnicodeDecodeError, json.JSONDecodeError,
                ValueError) as exc:
            raise SheinStoreAuthCryptoError("shein_store_decrypt_failed") from exc
        secret = str(payload.get("secretKey") or "")
        if not secret:
            raise SheinStoreAuthCryptoError("shein_store_secret_invalid")
        return secret
