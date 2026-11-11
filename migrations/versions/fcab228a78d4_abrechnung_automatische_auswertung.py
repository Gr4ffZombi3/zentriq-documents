"""Abrechnungen: automatische Mitarbeiteruebersicht (Details der Auswertung je Beleg)

Revision ID: fcab228a78d4
Revises: 96dd76af24da
Create Date: 2026-11-10 21:00:00.000000

Rein additiv: settlements.details (JSON). Bestehende Belege behalten ihre Zeilen, zaehlen aber erst
nach "Neu auswerten" (alte Auswertung mit Pruefliste).
"""

import sqlalchemy as sa
from alembic import op

revision = "fcab228a78d4"
down_revision = "96dd76af24da"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("settlements") as batch:
        batch.add_column(sa.Column("details", sa.JSON(), nullable=True))


def downgrade():
    with op.batch_alter_table("settlements") as batch:
        batch.drop_column("details")
