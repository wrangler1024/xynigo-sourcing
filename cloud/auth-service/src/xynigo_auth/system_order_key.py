"""Versioned, ASCII-only system order keys used across Xynigo services."""

from __future__ import annotations

import re


SYSTEM_ORDER_KEY_VERSION = "OK1"
SYSTEM_ORDER_KEY_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
SYSTEM_ORDER_KEY_RE = re.compile(
    r"^OK1-[0-9A-HJKMNP-TV-Z]{5}(?:-[0-9A-HJKMNP-TV-Z]{5}){3}$"
)

_FNV_OFFSET_64 = 0xCBF29CE484222325
_FNV_PRIME_64 = 0x100000001B3
_FNV_SECOND_SEED = _FNV_OFFSET_64 ^ 0x9E3779B97F4A7C15
_MASK_64 = (1 << 64) - 1
_IDENTITY_SEPARATOR = "\x1f"


def _normalize(value: object, *, upper: bool) -> str:
    normalized = " ".join(str(value or "").split())
    return normalized.upper() if upper else normalized.lower()


def canonical_order_identity(
    store_name: object,
    platform_order_no: object,
    package_id: object,
) -> str:
    """Return the private canonical identity used only as hash input."""

    return _IDENTITY_SEPARATOR.join((
        _normalize(store_name, upper=False),
        _normalize(platform_order_no, upper=True),
        _normalize(package_id, upper=True),
    ))


def legacy_order_key(
    store_name: object,
    platform_order_no: object,
    package_id: object,
) -> str:
    """Return the pre-OK1 compatibility key; never show it in business UI."""

    return "|".join((
        _normalize(store_name, upper=False),
        _normalize(platform_order_no, upper=True),
        _normalize(package_id, upper=True),
    ))


def _fnv1a64(data: bytes, seed: int) -> int:
    value = seed
    for byte in data:
        value ^= byte
        value = (value * _FNV_PRIME_64) & _MASK_64
    return value


def _crockford_base32(data: bytes) -> str:
    output: list[str] = []
    buffer = 0
    bits = 0
    for byte in data:
        buffer = (buffer << 8) | byte
        bits += 8
        while bits >= 5:
            bits -= 5
            output.append(SYSTEM_ORDER_KEY_ALPHABET[(buffer >> bits) & 31])
    if bits:
        output.append(SYSTEM_ORDER_KEY_ALPHABET[(buffer << (5 - bits)) & 31])
    return "".join(output)


def create_system_order_key(
    store_name: object,
    platform_order_no: object,
    package_id: object,
) -> str:
    """Create a stable ``OK1`` key with 96 bits of deterministic identity."""

    raw = canonical_order_identity(
        store_name,
        platform_order_no,
        package_id,
    ).encode("utf-8")
    forward = _fnv1a64(raw, _FNV_OFFSET_64).to_bytes(8, "big")
    reverse = _fnv1a64(raw[::-1], _FNV_SECOND_SEED).to_bytes(8, "big")
    payload = _crockford_base32(forward + reverse[:4])
    if len(payload) != 20:
        raise AssertionError("unexpected system order key payload length")
    return "%s-%s" % (
        SYSTEM_ORDER_KEY_VERSION,
        "-".join(payload[index:index + 5] for index in range(0, 20, 5)),
    )


def is_system_order_key(value: object) -> bool:
    return bool(SYSTEM_ORDER_KEY_RE.fullmatch(str(value or "").strip().upper()))
