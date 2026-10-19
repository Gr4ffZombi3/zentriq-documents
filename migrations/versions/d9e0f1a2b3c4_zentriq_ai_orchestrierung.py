"""ZentriqAI: Modellstufen, Verarbeitungsstatus, Doppelversand-Schutz, bestaetigte Aktionen

Revision ID: d9e0f1a2b3c4
Revises: c8d9e0f1a2b3
Create Date: 2026-10-19 22:00:00.000000

Rein additiv: neue Spalten ai_models.tier, ai_messages.progress/client_ref, neue Tabelle
ai_actions und ein neuer Enum-Wert AI_ACTION_EXECUTED fuer audit_logs.event_type. Bestehende Daten werden nicht veraendert (bekannte Startmodelle erhalten ihre Stufe).
"""

import sqlalchemy as sa
from alembic import op

revision = "d9e0f1a2b3c4"
down_revision = "c8d9e0f1a2b3"
branch_labels = None
depends_on = None

OLD_AUDIT_VALUES = (
    "LOGIN_SUCCESS", "LOGIN_FAILED", "LOGOUT", "PASSWORD_RESET_REQUESTED", "PASSWORD_RESET_COMPLETED",
    "USER_CREATED", "USER_UPDATED", "TIME_CORRECTED", "TIME_CORRECTION_REQUESTED", "TIME_CORRECTION_DECIDED",
    "USER_DELETED", "PASSWORD_CHANGED", "PASSWORD_RESET_TRIGGERED", "TWO_FACTOR_ENABLED", "TWO_FACTOR_RESET",
    "TWO_FACTOR_FAILED", "RECOVERY_CODE_USED", "RECOVERY_CODES_REGENERATED", "TENANT_CREATED", "TENANT_UPDATED",
    "TIME_CLOCK_IN", "TIME_CLOCK_OUT", "MEMO_TRANSCRIBED", "LEIPZIGER_LIST_UPLOADED", "SESSIONS_REVOKED",
    "CUSTOMER_MERGED", "MEMO_ASSIGNED", "CUSTOMER_CREATED", "ASSISTANT_USED", "AI_SETTINGS_CHANGED",
    "AI_CONVERSATION_DELETED",
)
NEW_AUDIT_VALUES = OLD_AUDIT_VALUES + ("AI_ACTION_EXECUTED",)


def _alter_audit_enum(from_values, to_values):
    with op.batch_alter_table("audit_logs", schema=None) as batch_op:
        batch_op.alter_column(
            "event_type",
            existing_type=sa.Enum(*from_values, name="auditeventtype"),
            type_=sa.Enum(*to_values, name="auditeventtype"),
            existing_nullable=False,
        )


TIERS = {
    ("gemini", "gemini-flash-latest"): 2,
    ("groq", "llama-3.3-70b-versatile"): 2,
    ("mistral", "mistral-medium-latest"): 2,
    ("anthropic", "claude-sonnet-5-5"): 2,
    ("anthropic", "claude-opus-5-5"): 3,
}


def upgrade():
    with op.batch_alter_table("ai_models", schema=None) as batch_op:
        batch_op.add_column(sa.Column("tier", sa.Integer(), nullable=False, server_default="1"))
    models = sa.table("ai_models", sa.column("provider", sa.String), sa.column("model_id", sa.String), sa.column("tier", sa.Integer))
    for (provider, model_id), tier in TIERS.items():
        op.get_bind().execute(
            models.update().where(models.c.provider == provider, models.c.model_id == model_id).values(tier=tier)
        )

    with op.batch_alter_table("ai_messages", schema=None) as batch_op:
        batch_op.add_column(sa.Column("progress", sa.String(length=100), nullable=True))
        batch_op.add_column(sa.Column("client_ref", sa.String(length=64), nullable=True))
        batch_op.create_index("ix_ai_messages_client_ref", ["client_ref"], unique=False)

    op.create_table(
        "ai_actions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("message_id", sa.Integer(), nullable=True),
        sa.Column("tool", sa.String(length=60), nullable=False),
        sa.Column("arguments", sa.JSON(), nullable=False),
        sa.Column("summary", sa.String(length=300), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("result", sa.String(length=300), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("decided_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["message_id"], ["ai_messages.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_ai_actions_tenant_id", "ai_actions", ["tenant_id"], unique=False)
    op.create_index("ix_ai_actions_user_id", "ai_actions", ["user_id"], unique=False)
    op.create_index("ix_ai_actions_message_id", "ai_actions", ["message_id"], unique=False)

    _alter_audit_enum(OLD_AUDIT_VALUES, NEW_AUDIT_VALUES)


def downgrade():
    # Schlaegt bewusst fehl, solange Audit-Eintraege mit dem neuen Event-Typ existieren.
    _alter_audit_enum(NEW_AUDIT_VALUES, OLD_AUDIT_VALUES)
    op.drop_table("ai_actions")
    with op.batch_alter_table("ai_messages", schema=None) as batch_op:
        batch_op.drop_index("ix_ai_messages_client_ref")
        batch_op.drop_column("client_ref")
        batch_op.drop_column("progress")
    with op.batch_alter_table("ai_models", schema=None) as batch_op:
        batch_op.drop_column("tier")
