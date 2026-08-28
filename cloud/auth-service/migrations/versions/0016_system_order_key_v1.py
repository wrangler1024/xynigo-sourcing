"""Add the ASCII-only OK1 system order key.

Revision ID: 0016_system_order_key_v1
Revises: 0015_executor_channel_p1
Create Date: 2026-08-28
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "0016_system_order_key_v1"
down_revision: str | None = "0015_executor_channel_p1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_FNV_OFFSET_64 = 0xCBF29CE484222325
_FNV_PRIME_64 = 0x100000001B3
_FNV_SECOND_SEED = _FNV_OFFSET_64 ^ 0x9E3779B97F4A7C15
_MASK_64 = (1 << 64) - 1


def _normalize(value: object, *, upper: bool) -> str:
    normalized = " ".join(str(value or "").split())
    return normalized.upper() if upper else normalized.lower()


def _fnv1a64(data: bytes, seed: int) -> int:
    value = seed
    for byte in data:
        value ^= byte
        value = (value * _FNV_PRIME_64) & _MASK_64
    return value


def _base32(data: bytes) -> str:
    output: list[str] = []
    buffer = 0
    bits = 0
    for byte in data:
        buffer = (buffer << 8) | byte
        bits += 8
        while bits >= 5:
            bits -= 5
            output.append(_ALPHABET[(buffer >> bits) & 31])
    if bits:
        output.append(_ALPHABET[(buffer << (5 - bits)) & 31])
    return "".join(output)


def _system_order_key(store: object, order_no: object, package_id: object) -> str:
    identity = "\x1f".join((
        _normalize(store, upper=False),
        _normalize(order_no, upper=True),
        _normalize(package_id, upper=True),
    )).encode("utf-8")
    forward = _fnv1a64(identity, _FNV_OFFSET_64).to_bytes(8, "big")
    reverse = _fnv1a64(identity[::-1], _FNV_SECOND_SEED).to_bytes(8, "big")
    payload = _base32(forward + reverse[:4])
    return "OK1-" + "-".join(
        payload[index:index + 5] for index in range(0, 20, 5)
    )


def upgrade() -> None:
    op.add_column(
        "purchase_orders",
        sa.Column("system_order_key", sa.String(length=32), nullable=True),
    )
    orders = sa.table(
        "purchase_orders",
        sa.column("id", sa.Uuid()),
        sa.column("tenant_id", sa.Uuid()),
        sa.column("store_name", sa.String(length=300)),
        sa.column("draft_payload", sa.JSON()),
        sa.column("system_order_key", sa.String(length=32)),
    )
    connection = op.get_bind()
    seen: set[tuple[object, str]] = set()
    for row in connection.execute(
        sa.select(
            orders.c.id,
            orders.c.tenant_id,
            orders.c.store_name,
            orders.c.draft_payload,
        )
    ).mappings():
        draft = dict(row["draft_payload"] or {})
        system_key = _system_order_key(
            row["store_name"] or draft.get("storeName"),
            draft.get("platformOrderNo"),
            draft.get("packageId"),
        )
        collision_key = (row["tenant_id"], system_key)
        if collision_key in seen:
            raise RuntimeError(
                "duplicate OK1 system order key during migration: " + system_key
            )
        seen.add(collision_key)
        draft["systemOrderKey"] = system_key
        connection.execute(
            orders.update()
            .where(orders.c.id == row["id"])
            .values(system_order_key=system_key, draft_payload=draft)
        )
    op.alter_column(
        "purchase_orders",
        "system_order_key",
        existing_type=sa.String(length=32),
        nullable=False,
    )
    op.create_unique_constraint(
        "uq_purchase_order_tenant_system_key",
        "purchase_orders",
        ["tenant_id", "system_order_key"],
    )


def downgrade() -> None:
    orders = sa.table(
        "purchase_orders",
        sa.column("id", sa.Uuid()),
        sa.column("draft_payload", sa.JSON()),
    )
    connection = op.get_bind()
    for row in connection.execute(
        sa.select(orders.c.id, orders.c.draft_payload)
    ).mappings():
        draft = dict(row["draft_payload"] or {})
        if "systemOrderKey" in draft:
            draft.pop("systemOrderKey", None)
            connection.execute(
                orders.update()
                .where(orders.c.id == row["id"])
                .values(draft_payload=draft)
            )
    op.drop_constraint(
        "uq_purchase_order_tenant_system_key",
        "purchase_orders",
        type_="unique",
    )
    op.drop_column("purchase_orders", "system_order_key")
