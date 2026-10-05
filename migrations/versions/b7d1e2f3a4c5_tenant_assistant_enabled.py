"""Mandanten: Freigabe des Textassistenten je Buero

Revision ID: b7d1e2f3a4c5
Revises: a1c2e3f4b5d6
Create Date: 2026-10-05 16:00:00.000000

Neue Spalte tenants.assistant_enabled (Standard: aus) - der Assistent ist nur fuer
freigegebene Bueros nutzbar. Bestehende Daten bleiben unveraendert; einmalig wird das Buero
"heller" freigegeben (bisher einziges produktives Buero, siehe flask align-tenant).
"""

import sqlalchemy as sa
from alembic import op

revision = "b7d1e2f3a4c5"
down_revision = "a1c2e3f4b5d6"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("tenants", schema=None) as batch_op:
        batch_op.add_column(sa.Column("assistant_enabled", sa.Boolean(), nullable=False, server_default=sa.false()))

    tenants = sa.table("tenants", sa.column("slug", sa.String), sa.column("assistant_enabled", sa.Boolean))
    op.get_bind().execute(tenants.update().where(tenants.c.slug == "heller").values(assistant_enabled=True))


def downgrade():
    with op.batch_alter_table("tenants", schema=None) as batch_op:
        batch_op.drop_column("assistant_enabled")
