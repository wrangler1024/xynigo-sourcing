"""Authenticated encryption for short-lived executor RPC payloads/results."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import zlib
from typing import Any

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


class ExecutorPayloadCipherError(RuntimeError):
    """Stable error that never includes task data or key material."""


class ExecutorPayloadCipher:
    PREFIX = b"XYER1"

    def __init__(self, deployment_key: str) -> None:
        try:
            material = base64.urlsafe_b64decode(
                str(deployment_key or "").encode("ascii")
            )
        except (ValueError, UnicodeEncodeError) as exc:
            raise ExecutorPayloadCipherError("executor_payload_key_invalid") from exc
        if len(material) != 32:
            raise ExecutorPayloadCipherError("executor_payload_key_invalid")
        key = hashlib.sha256(material + b"xynigo-executor-rpc-v1").digest()
        self._cipher = AESGCM(key)

    @staticmethod
    def _aad(tenant_id: object, task_id: object, purpose: str) -> bytes:
        return f"{tenant_id}:{task_id}:{purpose}".encode("ascii", errors="strict")

    def encrypt(
        self,
        payload: dict[str, Any],
        *,
        tenant_id: object,
        task_id: object,
        purpose: str,
    ) -> str:
        try:
            raw = json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            nonce = os.urandom(12)
            encrypted = self._cipher.encrypt(
                nonce,
                zlib.compress(raw, level=6),
                self._aad(tenant_id, task_id, purpose),
            )
            return base64.urlsafe_b64encode(
                self.PREFIX + nonce + encrypted
            ).decode("ascii")
        except Exception as exc:
            raise ExecutorPayloadCipherError("executor_payload_encrypt_failed") from exc

    def decrypt(
        self,
        ciphertext: str | None,
        *,
        tenant_id: object,
        task_id: object,
        purpose: str,
    ) -> dict[str, Any]:
        try:
            value = base64.urlsafe_b64decode(str(ciphertext or "").encode("ascii"))
        except (ValueError, UnicodeEncodeError) as exc:
            raise ExecutorPayloadCipherError(
                "executor_payload_ciphertext_invalid"
            ) from exc
        if not value.startswith(self.PREFIX) or len(value) <= len(self.PREFIX) + 12:
            raise ExecutorPayloadCipherError("executor_payload_ciphertext_invalid")
        offset = len(self.PREFIX)
        nonce = value[offset : offset + 12]
        encrypted = value[offset + 12 :]
        try:
            compressed = self._cipher.decrypt(
                nonce,
                encrypted,
                self._aad(tenant_id, task_id, purpose),
            )
            payload = json.loads(zlib.decompress(compressed).decode("utf-8"))
        except Exception as exc:
            raise ExecutorPayloadCipherError("executor_payload_decrypt_failed") from exc
        if not isinstance(payload, dict):
            raise ExecutorPayloadCipherError("executor_payload_invalid")
        return payload
