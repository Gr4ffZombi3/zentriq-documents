"""ZentriqAI: Anbieter, Modelle, Gespraeche, Nachrichten, Verbrauch

Revision ID: c8d9e0f1a2b3
Revises: b7d1e2f3a4c5
Create Date: 2026-10-19 20:00:00.000000

Rein additiv: fuenf neue Tabellen (ai_*) und zwei neue Enum-Werte fuer audit_logs.event_type.
Bestehende Tabellen und Daten werden nicht veraendert.
"""

import sqlalchemy as sa
from alembic import op

revision = "c8d9e0f1a2b3"
down_revision = "b7d1e2f3a4c5"
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
    "TIME_CLOCK_IN",
    "TIME_CLOCK_OUT",
    "MEMO_TRANSCRIBED",
    "LEIPZIGER_LIST_UPLOADED",
    "SESSIONS_REVOKED",
    "CUSTOMER_MERGED",
    "MEMO_ASSIGNED",
    "CUSTOMER_CREATED",
    "ASSISTANT_USED",
)
NEW_AUDIT_VALUES = OLD_AUDIT_VALUES + ("AI_SETTINGS_CHANGED", "AI_CONVERSATION_DELETED")


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
        "ai_provider_settings",
        sa.Column("provider", sa.String(length=40), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("customer_data_allowed", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("provider"),
    )
    op.create_table(
        "ai_models",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("provider", sa.String(length=40), nullable=False),
        sa.Column("model_id", sa.String(length=150), nullable=False),
        sa.Column("label", sa.String(length=150), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("priority", sa.Integer(), server_default="100", nullable=False),
        sa.Column("is_free", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("input_price", sa.Numeric(precision=10, scale=4), nullable=True),
        sa.Column("output_price", sa.Numeric(precision=10, scale=4), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("provider", "model_id", name="uq_ai_models_provider_model"),
    )
    op.create_index("ix_ai_models_provider", "ai_models", ["provider"], unique=False)

    op.create_table(
        "ai_conversations",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("model_choice", sa.String(length=200), server_default="auto", nullable=False),
        sa.Column("customer_data", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("summary_upto_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_ai_conversations_tenant_id", "ai_conversations", ["tenant_id"], unique=False)
    op.create_index("ix_ai_conversations_user_id", "ai_conversations", ["user_id"], unique=False)
    op.create_index("ix_ai_conversations_updated_at", "ai_conversations", ["updated_at"], unique=False)

    op.create_table(
        "ai_messages",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("conversation_id", sa.Integer(), nullable=False),
        sa.Column("role", sa.String(length=20), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("provider", sa.String(length=40), nullable=True),
        sa.Column("model", sa.String(length=150), nullable=True),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("cost_usd", sa.Numeric(precision=12, scale=6), nullable=True),
        sa.Column("cached", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["conversation_id"], ["ai_conversations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_ai_messages_tenant_id", "ai_messages", ["tenant_id"], unique=False)
    op.create_index("ix_ai_messages_conversation_id", "ai_messages", ["conversation_id"], unique=False)

    op.create_table(
        "ai_usage",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("conversation_id", sa.Integer(), nullable=True),
        sa.Column("provider", sa.String(length=40), nullable=False),
        sa.Column("model", sa.String(length=150), nullable=False),
        sa.Column("purpose", sa.String(length=20), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=False),
        sa.Column("output_tokens", sa.Integer(), nullable=False),
        sa.Column("cost_usd", sa.Numeric(precision=12, scale=6), nullable=False),
        sa.Column("ok", sa.Boolean(), nullable=False),
        sa.Column("error_type", sa.String(length=40), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["conversation_id"], ["ai_conversations.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_ai_usage_tenant_id", "ai_usage", ["tenant_id"], unique=False)
    op.create_index("ix_ai_usage_user_id", "ai_usage", ["user_id"], unique=False)
    op.create_index("ix_ai_usage_conversation_id", "ai_usage", ["conversation_id"], unique=False)
    op.create_index("ix_ai_usage_created_at", "ai_usage", ["created_at"], unique=False)

    _alter_audit_enum(OLD_AUDIT_VALUES, NEW_AUDIT_VALUES)


def downgrade():
    # Schlaegt bewusst fehl, solange Audit-Eintraege mit den neuen Event-Typen existieren.
    _alter_audit_enum(NEW_AUDIT_VALUES, OLD_AUDIT_VALUES)
    op.drop_table("ai_usage")
    op.drop_table("ai_messages")
    op.drop_table("ai_conversations")
    op.drop_table("ai_models")
    op.drop_table("ai_provider_settings")
