"""Audit-Events fuer Passwort-Reset

Revision ID: a9c3e5f7b1d2
Revises: d4e5f6a7b8c9
Create Date: 2026-09-25 20:00:00.000000

"""

import sqlalchemy as sa
from alembic import op

revision = "a9c3e5f7b1d2"
down_revision = "d4e5f6a7b8c9"
branch_labels = None
depends_on = None

OLD_VALUES = ("LOGIN_SUCCESS", "LOGIN_FAILED", "LOGOUT")
NEW_VALUES = OLD_VALUES + ("PASSWORD_RESET_REQUESTED", "PASSWORD_RESET_COMPLETED")


def upgrade():
    with op.batch_alter_table("audit_logs", schema=None) as batch_op:
        batch_op.alter_column(
            "event_type",
            existing_type=sa.Enum(*OLD_VALUES, name="auditeventtype"),
            type_=sa.Enum(*NEW_VALUES, name="auditeventtype"),
            existing_nullable=False,
        )


def downgrade():
    # Schlaegt fehl, solange Audit-Eintraege mit den neuen Event-Typen existieren - bewusst,
    # denn Audit-Eintraege werden nicht geloescht (Append-Only).
    with op.batch_alter_table("audit_logs", schema=None) as batch_op:
        batch_op.alter_column(
            "event_type",
            existing_type=sa.Enum(*NEW_VALUES, name="auditeventtype"),
            type_=sa.Enum(*OLD_VALUES, name="auditeventtype"),
            existing_nullable=False,
        )
