"""Store encrypted buyer credentials and complete source business fields.

Revision ID: 0012_buyer_account_credentials
Revises: 0011_buyer_db_base_mirror
Create Date: 2026-08-26
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "0012_buyer_account_credentials"
down_revision: str | None = "0011_buyer_db_base_mirror"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("buyer_accounts", sa.Column("credentials_ciphertext", sa.Text()))
    op.add_column(
        "buyer_accounts",
        sa.Column(
            "source_business_profile",
            sa.JSON(),
            server_default=sa.text("'{}'"),
            nullable=False,
        ),
    )
    op.alter_column("buyer_accounts", "source_business_profile", server_default=None)


def downgrade() -> None:
    op.drop_column("buyer_accounts", "source_business_profile")
    op.drop_column("buyer_accounts", "credentials_ciphertext")
