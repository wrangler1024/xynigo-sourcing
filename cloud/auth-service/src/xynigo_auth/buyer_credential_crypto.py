"""Authenticated application-layer encryption for buyer credentials."""

from __future__ import annotations

import json
from typing import Any

from cryptography.fernet import Fernet, InvalidToken


class BuyerCredentialError(RuntimeError):
    """Stable error without embedding encrypted or plaintext credential data."""


class BuyerCredentialCipher:
    """Encrypt compact JSON envelopes with a deployment-owned Fernet key."""

    PREFIX = "v1:"

    def __init__(self, key: str) -> None:
        normalized = str(key or "").strip().encode("ascii", errors="strict")
        if not normalized:
            raise BuyerCredentialError("buyer_credential_key_unavailable")
        try:
            self._fernet = Fernet(normalized)
        except (TypeError, ValueError) as exc:
            raise BuyerCredentialError("buyer_credential_key_invalid") from exc

    def encrypt(self, payload: dict[str, Any]) -> str:
        try:
            raw = json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            token = self._fernet.encrypt(raw).decode("ascii")
        except Exception as exc:
            raise BuyerCredentialError("buyer_credential_encrypt_failed") from exc
        return self.PREFIX + token

    def decrypt(self, ciphertext: str | None) -> dict[str, Any]:
        if not ciphertext:
            return {}
        value = str(ciphertext)
        if not value.startswith(self.PREFIX):
            raise BuyerCredentialError("buyer_credential_ciphertext_version")
        try:
            raw = self._fernet.decrypt(value[len(self.PREFIX) :].encode("ascii"))
            payload = json.loads(raw.decode("utf-8"))
        except (InvalidToken, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            raise BuyerCredentialError("buyer_credential_decrypt_failed") from exc
        if not isinstance(payload, dict):
            raise BuyerCredentialError("buyer_credential_payload_invalid")
        return payload
