"""Kundenstamm: Namensschluessel mit gefalteten Umlauten neu berechnen

Revision ID: a1c2e3f4b5d6
Revises: e8f9a0b1c2d3
Create Date: 2026-10-02 10:00:00.000000

name_key faltet Umlaute jetzt auf den Grundbuchstaben ("Löwenstein", "Loewenstein" und die
OCR-Lesart "Lowenstein" ergeben denselben Schluessel, siehe app/utils/customer_keys.py). Die
gespeicherten Schluessel werden einmalig neu berechnet - nur die Spalte name_key wird
geschrieben, keine Kundendaten. Keine Schemaaenderung.
"""

import sqlalchemy as sa
from alembic import op

revision = "a1c2e3f4b5d6"
down_revision = "e8f9a0b1c2d3"
branch_labels = None
depends_on = None


def upgrade():
    from app.utils.customer_keys import name_key

    connection = op.get_bind()
    customers = sa.table(
        "customers",
        sa.column("id", sa.Integer),
        sa.column("name", sa.String),
        sa.column("name_key", sa.String),
    )
    for customer_id, name, current in connection.execute(
        sa.select(customers.c.id, customers.c.name, customers.c.name_key)
    ).all():
        key = name_key(name)[:255] or None
        if key != current:
            connection.execute(customers.update().where(customers.c.id == customer_id).values(name_key=key))


def downgrade():
    # Die alten Schluessel sind eine reine Ableitung aus dem Namen; der neue Schluessel bleibt
    # auch mit dem alten Code verwendbar (er wird beim naechsten Speichern ohnehin neu gesetzt).
    pass
