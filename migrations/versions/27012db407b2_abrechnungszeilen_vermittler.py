"""Abrechnungszeilen: Vermittlernummer und Mitarbeiter je Tabellenblock

Revision ID: 27012db407b2
Revises: 1a987a9e7154
Create Date: 2026-11-09 22:00:00.000000

Rein additiv: settlement_lines.broker_number, settlement_lines.employee_user_id (nullable).
Bestehende Zeilen bleiben unveraendert (ohne Wert gilt weiter der Mitarbeiter des Belegs).
"""

import sqlalchemy as sa
from alembic import op

revision = "27012db407b2"
down_revision = "1a987a9e7154"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("settlement_lines") as batch:
        batch.add_column(sa.Column("broker_number", sa.String(length=20), nullable=True))
        batch.add_column(sa.Column("employee_user_id", sa.Integer(), nullable=True))
        batch.create_index("ix_settlement_lines_employee_user_id", ["employee_user_id"])
        batch.create_foreign_key("fk_settlement_lines_employee_user_id", "users", ["employee_user_id"], ["id"], ondelete="SET NULL")


def downgrade():
    with op.batch_alter_table("settlement_lines") as batch:
        batch.drop_constraint("fk_settlement_lines_employee_user_id", type_="foreignkey")
        batch.drop_index("ix_settlement_lines_employee_user_id")
        batch.drop_column("employee_user_id")
        batch.drop_column("broker_number")
