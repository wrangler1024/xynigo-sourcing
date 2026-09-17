"""SHEIN open-platform store authorization: credentials, links, events."""

import sqlalchemy as sa
from alembic import op

revision = "0043_shein_store_auth"
down_revision = "0042_after_sale_result_facts"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "shein_authorized_stores",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("tenant_id", sa.Uuid(),
                  sa.ForeignKey("tenants.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("merchant_id", sa.String(64), nullable=False,
                  server_default=""),
        sa.Column("store_name", sa.String(128), nullable=False),
        sa.Column("open_key_id", sa.String(64), nullable=False),
        sa.Column("secret_ciphertext", sa.Text(), nullable=False),
        sa.Column("app_id", sa.String(64), nullable=False),
        sa.Column("mode", sa.String(16), nullable=False),
        sa.Column("first_authorized_at", sa.DateTime(timezone=True),
                  nullable=False),
        sa.Column("latest_authorized_at", sa.DateTime(timezone=True),
                  nullable=False),
        sa.Column("last_verified_at", sa.DateTime(timezone=True)),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("store_info", sa.JSON(), nullable=False),
        sa.Column("created_by_user_id", sa.Uuid(),
                  sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.CheckConstraint("mode IN ('self', 'semi')",
                           name="ck_shein_store_mode"),
        sa.CheckConstraint("status IN ('pending', 'ok', 'expired')",
                           name="ck_shein_store_status"),
        # 匹配键用 openKeyId：换钥必然返回、且同店同应用跨重新授权稳定；
        # 商家ID 可能因店铺信息接口失败而暂缺，不能作唯一键（评审结论 20260917）。
        sa.UniqueConstraint("tenant_id", "open_key_id",
                            name="uq_shein_store_tenant_open_key"),
    )
    op.create_index(
        "ix_shein_store_tenant_merchant",
        "shein_authorized_stores",
        ["tenant_id", "merchant_id"],
    )
    op.create_index(
        "ix_shein_store_tenant_latest",
        "shein_authorized_stores",
        ["tenant_id", "latest_authorized_at"],
    )

    op.create_table(
        "shein_auth_links",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("tenant_id", sa.Uuid(),
                  sa.ForeignKey("tenants.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("created_by_user_id", sa.Uuid(),
                  sa.ForeignKey("users.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("state", sa.String(128), nullable=False),
        sa.Column("mode", sa.String(16), nullable=False),
        sa.Column("app_id", sa.String(64), nullable=False),
        sa.Column("target_store_id", sa.Uuid(),
                  sa.ForeignKey("shein_authorized_stores.id",
                                ondelete="SET NULL")),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.CheckConstraint("mode IN ('self', 'semi')",
                           name="ck_shein_auth_link_mode"),
        sa.UniqueConstraint("state", name="uq_shein_auth_link_state"),
    )
    op.create_index(
        "ix_shein_auth_link_tenant_created",
        "shein_auth_links",
        ["tenant_id", "created_at"],
    )

    op.create_table(
        "shein_auth_events",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("tenant_id", sa.Uuid(),
                  sa.ForeignKey("tenants.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("actor_user_id", sa.Uuid(),
                  sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("action", sa.String(32), nullable=False),
        sa.Column("store_id", sa.Uuid(),
                  sa.ForeignKey("shein_authorized_stores.id",
                                ondelete="SET NULL")),
        sa.Column("store_label", sa.String(128), nullable=False),
        sa.Column("ok", sa.Boolean(), nullable=False),
        sa.Column("note", sa.String(300), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.CheckConstraint(
            "action IN ('link', 'callback', 'verify', 'rename', 'delete')",
            name="ck_shein_auth_event_action"),
    )
    op.create_index(
        "ix_shein_auth_event_tenant_created",
        "shein_auth_events",
        ["tenant_id", "created_at"],
    )


def downgrade():
    op.drop_index("ix_shein_auth_event_tenant_created",
                  table_name="shein_auth_events")
    op.drop_table("shein_auth_events")
    op.drop_index("ix_shein_auth_link_tenant_created",
                  table_name="shein_auth_links")
    op.drop_table("shein_auth_links")
    op.drop_index("ix_shein_store_tenant_latest",
                  table_name="shein_authorized_stores")
    op.drop_table("shein_authorized_stores")
