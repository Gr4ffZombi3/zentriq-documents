"""Aktive Sitzungen, Aktivitaetsprotokoll-Ereignisse, optionale Kundennummer

Revision ID: f7a8b9c0d1e2
Revises: e5f6a7b8c9d0
Create Date: 2026-10-01 18:00:00.000000

Rein additiv: neue Tabelle user_sessions, neue Enum-Werte fuer audit_logs.event_type und die
nullable Spalte customers.customer_number. Bestehende Daten werden nicht veraendert.
"""

import sqlalchemy as sa
from alembic import op

revision = "f7a8b9c0d1e2"
down_revision = "e5f6a7b8c9d0"
branch_labels = None
depends_on = None

OLD_AUDIT_VALUES = (
    "LOGIN_SUCCESS",
    "LOGIN_FAILED",
    "LOGOUT",
    "PASSWORD_RESET_REQUESTED",
    "PASSWORD_RESET_COMPLETED",
    "USER_CREATED",
    "USER_UPDATED",
    "TIME_CORRECTED",
    "TIME_CORRECTION_REQUESTED",
    "TIME_CORRECTION_DECIDED",
    "USER_DELETED",
    "PASSWORD_CHANGED",
    "PASSWORD_RESET_TRIGGERED",
    "TWO_FACTOR_ENABLED",
    "TWO_FACTOR_RESET",
    "TWO_FACTOR_FAILED",
    "RECOVERY_CODE_USED",
    "RECOVERY_CODES_REGENERATED",
    "TENANT_CREATED",
    "TENANT_UPDATED",
)
NEW_AUDIT_VALUES = OLD_AUDIT_VALUES + (
    "TIME_CLOCK_IN",
    "TIME_CLOCK_OUT",
    "MEMO_TRANSCRIBED",
    "LEIPZIGER_LIST_UPLOADED",
    "SESSIONS_REVOKED",
)


def _alter_audit_enum(from_values, to_values):
    with op.batch_alter_table("audit_logs", schema=None) as batch_op:
        batch_op.alter_column(
            "event_type",
            existing_type=sa.Enum(*from_values, name="auditeventtype"),
            type_=sa.Enum(*to_values, name="auditeventtype"),
            existing_nullable=False,
        )


def upgrade():
    op.create_table(
        "user_sessions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(), nullable=False),
        sa.Column("ip_address", sa.String(length=64), nullable=True),
        sa.Column("user_agent", sa.String(length=255), nullable=True),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_hash"),
    )
    with op.batch_alter_table("user_sessions", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_user_sessions_user_id"), ["user_id"], unique=False)

    _alter_audit_enum(OLD_AUDIT_VALUES, NEW_AUDIT_VALUES)

    with op.batch_alter_table("customers", schema=None) as batch_op:
        batch_op.add_column(sa.Column("customer_number", sa.String(length=50), nullable=True))
        batch_op.create_index(batch_op.f("ix_customers_customer_number"), ["customer_number"], unique=False)


def downgrade():
    with op.batch_alter_table("customers", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_customers_customer_number"))
        batch_op.drop_column("customer_number")
    # Schlaegt bewusst fehl, solange Audit-Eintraege mit den neuen Event-Typen existieren.
    _alter_audit_enum(NEW_AUDIT_VALUES, OLD_AUDIT_VALUES)
    op.drop_table("user_sessions")
