"""Keep unknown platform write results distinct from retryable failures."""
from alembic import op

revision = '0041_after_sale_uncertain'
down_revision = '0040_after_sale_refunds'
branch_labels = None
depends_on = None

OLD = "status IN ('ok','empty','skip','blocked','fail','login','inuse','stopped','queued','running')"
NEW = "status IN ('ok','empty','skip','blocked','fail','login','inuse','stopped','queued','running','uncertain','verifying')"


def upgrade():
    with op.batch_alter_table('after_sale_claim_results') as batch:
        batch.drop_constraint('ck_after_sale_result_status', type_='check')
        batch.create_check_constraint('ck_after_sale_result_status', NEW)


def downgrade():
    # Do not silently turn an unknown write into a retryable failure on rollback.
    connection = op.get_bind()
    import sqlalchemy as sa
    if connection.execute(sa.text("SELECT COUNT(*) FROM after_sale_claim_results WHERE status IN ('uncertain','verifying')")).scalar():
        raise RuntimeError('Resolve uncertain after-sale records before downgrading')
    with op.batch_alter_table('after_sale_claim_results') as batch:
        batch.drop_constraint('ck_after_sale_result_status', type_='check')
        batch.create_check_constraint('ck_after_sale_result_status', OLD)
