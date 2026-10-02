"""Listenvergleich: Enum-Wert NEW_PRODUCT_LINE fuer list_comparison_entries.change_type

Revision ID: e8f9a0b1c2d3
Revises: d6e7f8a9b0c1
Create Date: 2026-10-02 09:00:00.000000

M12 hat ListChangeType.NEW_PRODUCT_LINE ("Neue Sparte erkannt") im Code eingefuehrt, die
Migration c7274613e41d aber nur die Zaehlspalte new_product_line_count angelegt - der
MariaDB-Enum der Spalte change_type kannte den Wert nie. Jeder Listenvergleich mit einer neuen
Sparte scheiterte deshalb beim Speichern ("Data truncated for column 'change_type'").

Rein additiv: erweitert nur die erlaubten Werte, bestehende Zeilen bleiben unveraendert.
"""

import sqlalchemy as sa
from alembic import op

revision = "e8f9a0b1c2d3"
down_revision = "d6e7f8a9b0c1"
branch_labels = None
depends_on = None

OLD_VALUES = ("NEW_CUSTOMER", "NEW_CONTRACT", "NEW_OFFER", "STATUS_CHANGE", "STORNO", "REMOVED_CUSTOMER")
NEW_VALUES = OLD_VALUES + ("NEW_PRODUCT_LINE",)


def _alter_change_type_enum(from_values, to_values):
    with op.batch_alter_table("list_comparison_entries", schema=None) as batch_op:
        batch_op.alter_column(
            "change_type",
            existing_type=sa.Enum(*from_values, name="listchangetype"),
            type_=sa.Enum(*to_values, name="listchangetype"),
            existing_nullable=False,
        )


def upgrade():
    _alter_change_type_enum(OLD_VALUES, NEW_VALUES)


def downgrade():
    # Schlaegt bewusst fehl, solange Eintraege mit NEW_PRODUCT_LINE existieren.
    _alter_change_type_enum(NEW_VALUES, OLD_VALUES)
