"""Domain-separated encryption for tenant data-source registries."""

from __future__ import annotations

import base64
import hashlib
import json
import uuid
from typing import Any

from cryptography.fernet import Fernet, InvalidToken


class DataSourceRegistryCipherError(RuntimeError):
    pass


class DataSourceRegistryCipher:
    PREFIX = "dsr1:"

    def __init__(self, deployment_key: str) -> None:
        try:
            material = base64.urlsafe_b64decode(
                str(deployment_key or "").encode("ascii")
            )
        except (ValueError, UnicodeEncodeError) as exc:
            raise DataSourceRegistryCipherError(
                "data_source_registry_key_invalid"
            ) from exc
        if len(material) != 32:
            raise DataSourceRegistryCipherError("data_source_registry_key_invalid")
        derived = hashlib.sha256(
            material + b"xynigo-data-source-registry-v1"
        ).digest()
        self._fernet = Fernet(base64.urlsafe_b64encode(derived))

    def encrypt(self, payload: dict[str, Any], *, tenant_id: uuid.UUID) -> str:
        envelope = {
            "tenantId": str(tenant_id),
            "registry": payload,
        }
        try:
            raw = json.dumps(
                envelope,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            return self.PREFIX + self._fernet.encrypt(raw).decode("ascii")
        except Exception as exc:
            raise DataSourceRegistryCipherError(
                "data_source_registry_encrypt_failed"
            ) from exc

    def decrypt(
        self, ciphertext: str | None, *, tenant_id: uuid.UUID
    ) -> dict[str, Any]:
        value = str(ciphertext or "")
        if not value.startswith(self.PREFIX):
            raise DataSourceRegistryCipherError(
                "data_source_registry_ciphertext_invalid"
            )
        try:
            raw = self._fernet.decrypt(value[len(self.PREFIX) :].encode("ascii"))
            envelope = json.loads(raw.decode("utf-8"))
        except (InvalidToken, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            raise DataSourceRegistryCipherError(
                "data_source_registry_decrypt_failed"
            ) from exc
        if (
            not isinstance(envelope, dict)
            or envelope.get("tenantId") != str(tenant_id)
            or not isinstance(envelope.get("registry"), dict)
        ):
            raise DataSourceRegistryCipherError(
                "data_source_registry_payload_invalid"
            )
        return envelope["registry"]
