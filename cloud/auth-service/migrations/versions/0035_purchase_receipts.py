"""Purchase evidence and durable write claims."""
import sqlalchemy as sa
from alembic import op
revision = '0035_purchase_receipts'
down_revision = '0034_store_finance_lookup'
branch_labels = None
depends_on = None

def upgrade():
    op.create_table('purchase_receipts',
        sa.Column('id', sa.Uuid(), primary_key=True),
        sa.Column('tenant_id', sa.Uuid(), sa.ForeignKey('tenants.id'), nullable=False),
        sa.Column('actor_id', sa.Uuid(), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('request_id', sa.String(64), nullable=False),
        sa.Column('revision', sa.Integer(), nullable=False),
        sa.Column('state', sa.String(24), nullable=False),
        sa.Column('fingerprint', sa.String(64), nullable=False),
        sa.Column('order_no', sa.String(64), nullable=False),
        sa.Column('amount', sa.String(24), nullable=False),
        sa.Column('currency', sa.String(3), nullable=False),
        sa.Column('paid_at', sa.String(40), nullable=False),
        sa.Column('reason', sa.String(300), nullable=False),
        sa.Column('image', sa.LargeBinary(), nullable=False),
        sa.Column('image_hash', sa.String(64), nullable=False),
        sa.Column('image_cells', sa.JSON(), nullable=False),
        sa.Column('previous_id', sa.Uuid(), sa.ForeignKey('purchase_receipts.id')),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint('tenant_id', 'request_id', name='uq_purchase_receipt_request'))
    op.create_table('purchase_receipt_slots',
        sa.Column('tenant_id', sa.Uuid(), sa.ForeignKey('tenants.id'), primary_key=True),
        sa.Column('task_hash', sa.String(64), primary_key=True),
        sa.Column('revision', sa.Integer(), nullable=False),
        sa.Column('receipt_id', sa.Uuid(), sa.ForeignKey('purchase_receipts.id')))

    op.create_table('purchase_receipt_orders',
        sa.Column('tenant_id', sa.Uuid(), sa.ForeignKey('tenants.id'), primary_key=True),
        sa.Column('order_no', sa.String(64), primary_key=True),
        sa.Column('task_hash', sa.String(64), nullable=False))

def downgrade():
    op.drop_table('purchase_receipt_orders')
    op.drop_table('purchase_receipt_slots')
    op.drop_table('purchase_receipts')
