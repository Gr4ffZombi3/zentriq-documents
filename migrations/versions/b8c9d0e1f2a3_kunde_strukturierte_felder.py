"""Kunde: strukturierte Felder Vorname, Nachname, Strasse, Hausnummer

Abwaertskompatibel: alle Spalten optional; `name` und `address` bleiben die fuehrenden
Anzeigefelder und werden bei strukturierter Erfassung mitgepflegt.

Revision ID: b8c9d0e1f2a3
Revises: a7b8c9d0e1f2
"""

import sqlalchemy as sa
from alembic import op

revision = "b8c9d0e1f2a3"
down_revision = "a7b8c9d0e1f2"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("customers") as batch:
        batch.add_column(sa.Column("first_name", sa.String(120), nullable=True))
        batch.add_column(sa.Column("last_name", sa.String(120), nullable=True))
        batch.add_column(sa.Column("street", sa.String(200), nullable=True))
        batch.add_column(sa.Column("house_number", sa.String(20), nullable=True))


def downgrade():
    with op.batch_alter_table("customers") as batch:
        batch.drop_column("house_number")
        batch.drop_column("street")
        batch.drop_column("last_name")
        batch.drop_column("first_name")
