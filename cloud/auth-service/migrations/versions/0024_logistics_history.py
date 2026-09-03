"""Add durable logistics-query lineage and history indexes.

Revision ID: 0024_logistics_history
Revises: 0023_tenant_feishu_integration
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from alembic import op


revision: str = "0024_logistics_history"
down_revision: str | None = "0023_tenant_feishu_integration"
branch_labels = None
depends_on = None


def _uuid_or_none(value: object) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(value)) if value else None
    except (TypeError, ValueError):
        return None


def upgrade() -> None:
    op.add_column(
        "logistics_query_runs",
        sa.Column("parent_run_id", sa.Uuid(), nullable=True),
    )
    op.add_column(
        "logistics_query_runs",
        sa.Column("root_run_id", sa.Uuid(), nullable=True),
    )
    op.create_foreign_key(
        "fk_logistics_query_run_parent",
        "logistics_query_runs",
        "logistics_query_runs",
        ["parent_run_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_logistics_query_run_root",
        "logistics_query_runs",
        "logistics_query_runs",
        ["root_run_id"],
        ["id"],
        ondelete="SET NULL",
    )

    bind = op.get_bind()
    runs = sa.table(
        "logistics_query_runs",
        sa.column("id", sa.Uuid()),
        sa.column("tenant_id", sa.Uuid()),
        sa.column("actor_user_id", sa.Uuid()),
        sa.column("request_summary", sa.JSON()),
        sa.column("parent_run_id", sa.Uuid()),
        sa.column("root_run_id", sa.Uuid()),
    )
    rows = list(
        bind.execute(
            sa.select(
                runs.c.id,
                runs.c.tenant_id,
                runs.c.actor_user_id,
                runs.c.request_summary,
            )
        ).mappings()
    )
    identities = {
        row["id"]: (row["tenant_id"], row["actor_user_id"])
        for row in rows
    }
    parents: dict[uuid.UUID, uuid.UUID | None] = {}
    for row in rows:
        summary = row["request_summary"] if isinstance(row["request_summary"], dict) else {}
        parent_id = _uuid_or_none(summary.get("parentRunId"))
        if (
            parent_id not in identities
            or identities[parent_id] != identities[row["id"]]
            or parent_id == row["id"]
        ):
            parent_id = None
        parents[row["id"]] = parent_id

    def root_for(run_id: uuid.UUID) -> uuid.UUID:
        current = run_id
        visited = {run_id}
        for _ in range(100):
            parent_id = parents.get(current)
            if parent_id is None or parent_id in visited:
                return current
            visited.add(parent_id)
            current = parent_id
        return current

    for run_id, parent_id in parents.items():
        bind.execute(
            runs.update()
            .where(runs.c.id == run_id)
            .values(parent_run_id=parent_id, root_run_id=root_for(run_id))
        )

    op.create_index(
        "ix_logistics_run_history_root",
        "logistics_query_runs",
        ["tenant_id", "actor_user_id", "parent_run_id", "created_at", "id"],
    )
    op.create_index(
        "ix_logistics_run_history_descendant",
        "logistics_query_runs",
        ["tenant_id", "actor_user_id", "root_run_id", "created_at", "id"],
    )


def downgrade() -> None:
    op.drop_index("ix_logistics_run_history_descendant", table_name="logistics_query_runs")
    op.drop_index("ix_logistics_run_history_root", table_name="logistics_query_runs")
    op.drop_constraint(
        "fk_logistics_query_run_root",
        "logistics_query_runs",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_logistics_query_run_parent",
        "logistics_query_runs",
        type_="foreignkey",
    )
    op.drop_column("logistics_query_runs", "root_run_id")
    op.drop_column("logistics_query_runs", "parent_run_id")
