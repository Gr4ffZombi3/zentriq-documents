"""Kunden: Festnetz und Privattelefon

Revision ID: df2d59947081
Revises: 27012db407b2
Create Date: 2026-11-10 09:00:00.000000

Rein additiv: customers.phone_landline, phone_private und ihre Suchschluessel. Die bisherige
Spalte phone bleibt unveraendert (Typ unbekannt, weiter angezeigt und durchsuchbar).
"""

import sqlalchemy as sa
from alembic import op

revision = "df2d59947081"
down_revision = "27012db407b2"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("customers") as batch:
        batch.add_column(sa.Column("phone_landline", sa.String(length=50), nullable=True))
        batch.add_column(sa.Column("phone_private", sa.String(length=50), nullable=True))
        batch.add_column(sa.Column("phone_landline_key", sa.String(length=20), nullable=True))
        batch.add_column(sa.Column("phone_private_key", sa.String(length=20), nullable=True))
        batch.create_index("ix_customers_tenant_phone_landline_key", ["tenant_id", "phone_landline_key"])
        batch.create_index("ix_customers_tenant_phone_private_key", ["tenant_id", "phone_private_key"])


def downgrade():
    with op.batch_alter_table("customers") as batch:
        batch.drop_index("ix_customers_tenant_phone_private_key")
        batch.drop_index("ix_customers_tenant_phone_landline_key")
        batch.drop_column("phone_private_key")
        batch.drop_column("phone_landline_key")
        batch.drop_column("phone_private")
        batch.drop_column("phone_landline")
