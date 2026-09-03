"""Domain-separated encryption for tenant integration credentials."""

from __future__ import annotations

import base64
import hashlib
import json
from typing import Any

from cryptography.fernet import Fernet, InvalidToken


class TenantIntegrationCipherError(RuntimeError):
    pass


class TenantIntegrationCipher:
    PREFIX = "v1:"

    def __init__(self, deployment_key: str) -> None:
        try:
            material = base64.urlsafe_b64decode(str(deployment_key or "").encode("ascii"))
        except (ValueError, UnicodeEncodeError) as exc:
            raise TenantIntegrationCipherError("tenant_integration_key_invalid") from exc
        if len(material) != 32:
            raise TenantIntegrationCipherError("tenant_integration_key_invalid")
        derived = hashlib.sha256(material + b"xynigo-tenant-integration-v1").digest()
        self._fernet = Fernet(base64.urlsafe_b64encode(derived))

    def encrypt(self, payload: dict[str, Any]) -> str:
        try:
            raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
            return self.PREFIX + self._fernet.encrypt(raw).decode("ascii")
        except Exception as exc:
            raise TenantIntegrationCipherError("tenant_integration_encrypt_failed") from exc

    def decrypt(self, ciphertext: str | None) -> dict[str, Any]:
        value = str(ciphertext or "")
        if not value.startswith(self.PREFIX):
            raise TenantIntegrationCipherError("tenant_integration_ciphertext_invalid")
        try:
            raw = self._fernet.decrypt(value[len(self.PREFIX):].encode("ascii"))
            payload = json.loads(raw.decode("utf-8"))
        except (InvalidToken, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            raise TenantIntegrationCipherError("tenant_integration_decrypt_failed") from exc
        if not isinstance(payload, dict):
            raise TenantIntegrationCipherError("tenant_integration_payload_invalid")
        return payload
