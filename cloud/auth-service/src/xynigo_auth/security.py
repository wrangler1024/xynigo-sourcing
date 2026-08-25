from __future__ import annotations

import base64
import hashlib
import secrets


def random_url_token(byte_length: int = 32) -> str:
    return secrets.token_urlsafe(byte_length)


def hash_token(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def pkce_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
