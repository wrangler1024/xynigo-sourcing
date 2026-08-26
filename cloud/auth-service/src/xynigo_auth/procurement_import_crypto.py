"""Domain-separated authenticated encryption for short-lived import plans."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import zlib
from typing import Any

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


class ProcurementImportCipherError(RuntimeError):
    """Stable error that never contains a plan payload or encryption key."""


class ProcurementImportCipher:
    PREFIX = b"XYPI1"

    def __init__(self, deployment_key: str) -> None:
        try:
            material = base64.urlsafe_b64decode(str(deployment_key or "").encode("ascii"))
        except (ValueError, UnicodeEncodeError) as exc:
            raise ProcurementImportCipherError("procurement_import_key_invalid") from exc
        if len(material) != 32:
            raise ProcurementImportCipherError("procurement_import_key_invalid")
        key = hashlib.sha256(material + b"xynigo-procurement-import-v1").digest()
        self._cipher = AESGCM(key)

    @staticmethod
    def _aad(tenant_id: object, plan_id: object) -> bytes:
        return f"{tenant_id}:{plan_id}".encode("ascii", errors="strict")

    def encrypt(self, payload: dict[str, Any], *, tenant_id: object, plan_id: object) -> bytes:
        try:
            raw = json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            compressed = zlib.compress(raw, level=6)
            nonce = os.urandom(12)
            encrypted = self._cipher.encrypt(
                nonce, compressed, self._aad(tenant_id, plan_id)
            )
            return self.PREFIX + nonce + encrypted
        except Exception as exc:
            raise ProcurementImportCipherError(
                "procurement_import_encrypt_failed"
            ) from exc

    def decrypt(
        self, ciphertext: bytes | None, *, tenant_id: object, plan_id: object
    ) -> dict[str, Any]:
        value = bytes(ciphertext or b"")
        if not value.startswith(self.PREFIX) or len(value) <= len(self.PREFIX) + 12:
            raise ProcurementImportCipherError("procurement_import_ciphertext_invalid")
        nonce_start = len(self.PREFIX)
        nonce = value[nonce_start : nonce_start + 12]
        encrypted = value[nonce_start + 12 :]
        try:
            compressed = self._cipher.decrypt(
                nonce, encrypted, self._aad(tenant_id, plan_id)
            )
            payload = json.loads(zlib.decompress(compressed).decode("utf-8"))
        except Exception as exc:
            raise ProcurementImportCipherError(
                "procurement_import_decrypt_failed"
            ) from exc
        if not isinstance(payload, dict):
            raise ProcurementImportCipherError("procurement_import_payload_invalid")
        return payload
