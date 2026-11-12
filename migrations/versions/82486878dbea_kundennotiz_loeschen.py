"""Kundennotizen loeschen: Protokolleintrag CUSTOMER_NOTE_DELETED

Revision ID: 82486878dbea
Revises: 026d11b8a1f1
Create Date: 2026-11-12 12:00:00.000000

Rein additiv: ein neuer Enum-Wert CUSTOMER_NOTE_DELETED fuer audit_logs.event_type (nur IDs,
kein Notiztext). Bestehende Daten werden nicht veraendert.
"""

import sqlalchemy as sa
from alembic import op

revision = "82486878dbea"
down_revision = "026d11b8a1f1"
branch_labels = None
depends_on = None

OLD_AUDIT_VALUES = (
    "LOGIN_SUCCESS", "LOGIN_FAILED", "LOGOUT", "PASSWORD_RESET_REQUESTED", "PASSWORD_RESET_COMPLETED",
    "USER_CREATED", "USER_UPDATED", "TIME_CORRECTED", "TIME_CORRECTION_REQUESTED", "TIME_CORRECTION_DECIDED",
    "USER_DELETED", "PASSWORD_CHANGED", "PASSWORD_RESET_TRIGGERED", "TWO_FACTOR_ENABLED", "TWO_FACTOR_RESET",
    "TWO_FACTOR_FAILED", "RECOVERY_CODE_USED", "RECOVERY_CODES_REGENERATED", "TENANT_CREATED", "TENANT_UPDATED",
    "TIME_CLOCK_IN", "TIME_CLOCK_OUT", "MEMO_TRANSCRIBED", "LEIPZIGER_LIST_UPLOADED", "SESSIONS_REVOKED",
    "CUSTOMER_MERGED", "MEMO_ASSIGNED", "CUSTOMER_CREATED", "ASSISTANT_USED", "AI_SETTINGS_CHANGED",
    "AI_CONVERSATION_DELETED", "AI_ACTION_EXECUTED",
)
NEW_AUDIT_VALUES = OLD_AUDIT_VALUES + ("CUSTOMER_NOTE_DELETED",)


def _alter_audit_enum(from_values, to_values):
    with op.batch_alter_table("audit_logs", schema=None) as batch_op:
        batch_op.alter_column(
            "event_type",
            existing_type=sa.Enum(*from_values, name="auditeventtype"),
            type_=sa.Enum(*to_values, name="auditeventtype"),
            existing_nullable=False,
        )


def upgrade():
    _alter_audit_enum(OLD_AUDIT_VALUES, NEW_AUDIT_VALUES)


def downgrade():
    _alter_audit_enum(NEW_AUDIT_VALUES, OLD_AUDIT_VALUES)
