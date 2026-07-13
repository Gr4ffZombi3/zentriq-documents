"""tenants and tenant_id scoping on customers, documents, document_customers, recommendations

Revision ID: 355ad74c6220
Revises: 1354c157c9a1
Create Date: 2026-07-13 22:41:08.176898

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '355ad74c6220'
down_revision = '1354c157c9a1'
branch_labels = None
depends_on = None

DEFAULT_TENANT_SLUG = "default"


def upgrade():
    op.create_table('tenants',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('name', sa.String(length=255), nullable=False),
    sa.Column('slug', sa.String(length=100), nullable=False),
    sa.Column('status', sa.Enum('ACTIVE', 'SUSPENDED', name='tenantstatus'), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=True),
    sa.Column('updated_at', sa.DateTime(), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('tenants', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_tenants_slug'), ['slug'], unique=True)

    # Bestehende Installationen (vor Multi-Tenancy) bekommen einen "Default Tenant", auf
    # den alle bereits vorhandenen Zeilen zurueckdatiert werden, bevor tenant_id NOT NULL wird.
    tenants_table = sa.table(
        'tenants',
        sa.column('id', sa.Integer),
        sa.column('name', sa.String),
        sa.column('slug', sa.String),
        sa.column('status', sa.String),
    )
    op.bulk_insert(
        tenants_table,
        [{'name': 'Default Tenant', 'slug': DEFAULT_TENANT_SLUG, 'status': 'ACTIVE'}],
    )
    connection = op.get_bind()
    default_tenant_id = connection.execute(
        sa.text("SELECT id FROM tenants WHERE slug = :slug"), {"slug": DEFAULT_TENANT_SLUG}
    ).scalar_one()

    # customers: Spalte nullable hinzufuegen, backfuellen, dann NOT NULL erzwingen.
    with op.batch_alter_table('customers', schema=None) as batch_op:
        batch_op.add_column(sa.Column('tenant_id', sa.Integer(), nullable=True))
    connection.execute(sa.text("UPDATE customers SET tenant_id = :tid"), {"tid": default_tenant_id})
    with op.batch_alter_table('customers', schema=None) as batch_op:
        batch_op.alter_column('tenant_id', existing_type=sa.Integer(), nullable=False)
        batch_op.create_index(batch_op.f('ix_customers_tenant_id'), ['tenant_id'], unique=False)
        batch_op.create_foreign_key('fk_customers_tenant_id_tenants', 'tenants', ['tenant_id'], ['id'])

    # documents
    with op.batch_alter_table('documents', schema=None) as batch_op:
        batch_op.add_column(sa.Column('tenant_id', sa.Integer(), nullable=True))
    connection.execute(sa.text("UPDATE documents SET tenant_id = :tid"), {"tid": default_tenant_id})
    with op.batch_alter_table('documents', schema=None) as batch_op:
        batch_op.alter_column('tenant_id', existing_type=sa.Integer(), nullable=False)
        batch_op.create_index(batch_op.f('ix_documents_tenant_id'), ['tenant_id'], unique=False)
        batch_op.create_foreign_key('fk_documents_tenant_id_tenants', 'tenants', ['tenant_id'], ['id'])

    # document_customers (+ Unique Constraint auf tenant_id erweitern)
    with op.batch_alter_table('document_customers', schema=None) as batch_op:
        batch_op.add_column(sa.Column('tenant_id', sa.Integer(), nullable=True))
    connection.execute(sa.text("UPDATE document_customers SET tenant_id = :tid"), {"tid": default_tenant_id})
    with op.batch_alter_table('document_customers', schema=None) as batch_op:
        batch_op.alter_column('tenant_id', existing_type=sa.Integer(), nullable=False)
        batch_op.drop_constraint(batch_op.f('uq_document_customer'), type_='unique')
        batch_op.create_index(batch_op.f('ix_document_customers_tenant_id'), ['tenant_id'], unique=False)
        batch_op.create_unique_constraint('uq_document_customer_tenant', ['tenant_id', 'document_id', 'customer_id'])
        batch_op.create_foreign_key('fk_document_customers_tenant_id_tenants', 'tenants', ['tenant_id'], ['id'])

    # recommendations
    with op.batch_alter_table('recommendations', schema=None) as batch_op:
        batch_op.add_column(sa.Column('tenant_id', sa.Integer(), nullable=True))
    connection.execute(sa.text("UPDATE recommendations SET tenant_id = :tid"), {"tid": default_tenant_id})
    with op.batch_alter_table('recommendations', schema=None) as batch_op:
        batch_op.alter_column('tenant_id', existing_type=sa.Integer(), nullable=False)
        batch_op.create_index(batch_op.f('ix_recommendations_tenant_id'), ['tenant_id'], unique=False)
        batch_op.create_foreign_key('fk_recommendations_tenant_id_tenants', 'tenants', ['tenant_id'], ['id'])


def downgrade():
    with op.batch_alter_table('recommendations', schema=None) as batch_op:
        batch_op.drop_constraint('fk_recommendations_tenant_id_tenants', type_='foreignkey')
        batch_op.drop_index(batch_op.f('ix_recommendations_tenant_id'))
        batch_op.drop_column('tenant_id')

    with op.batch_alter_table('documents', schema=None) as batch_op:
        batch_op.drop_constraint('fk_documents_tenant_id_tenants', type_='foreignkey')
        batch_op.drop_index(batch_op.f('ix_documents_tenant_id'))
        batch_op.drop_column('tenant_id')

    with op.batch_alter_table('document_customers', schema=None) as batch_op:
        batch_op.drop_constraint('fk_document_customers_tenant_id_tenants', type_='foreignkey')
        batch_op.drop_constraint('uq_document_customer_tenant', type_='unique')
        batch_op.drop_index(batch_op.f('ix_document_customers_tenant_id'))
        batch_op.create_unique_constraint(batch_op.f('uq_document_customer'), ['document_id', 'customer_id'])
        batch_op.drop_column('tenant_id')

    with op.batch_alter_table('customers', schema=None) as batch_op:
        batch_op.drop_constraint('fk_customers_tenant_id_tenants', type_='foreignkey')
        batch_op.drop_index(batch_op.f('ix_customers_tenant_id'))
        batch_op.drop_column('tenant_id')

    with op.batch_alter_table('tenants', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_tenants_slug'))

    op.drop_table('tenants')
