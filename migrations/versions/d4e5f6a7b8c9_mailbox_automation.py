"""Mailbox-Automation mit HUK-Rueckrufhistorie

Revision ID: d4e5f6a7b8c9
Revises: f1b2c3d4e5f6
Create Date: 2026-09-25 18:00:00.000000

"""

from alembic import op
import sqlalchemy as sa


revision = "d4e5f6a7b8c9"
down_revision = "f1b2c3d4e5f6"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "mailbox_cases",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("source_key", sa.String(length=128), nullable=False),
        sa.Column("source_message_id", sa.String(length=512), nullable=True),
        sa.Column("source_uid", sa.String(length=128), nullable=True),
        sa.Column("source_mailbox", sa.String(length=255), nullable=True),
        sa.Column("source_sender", sa.String(length=512), nullable=True),
        sa.Column("source_subject", sa.String(length=998), nullable=True),
        sa.Column("received_at", sa.DateTime(), nullable=True),
        sa.Column("audio_filename", sa.String(length=255), nullable=True),
        sa.Column("audio_content_type", sa.String(length=128), nullable=True),
        sa.Column("audio_sha256", sa.String(length=64), nullable=True),
        sa.Column("audio_size_bytes", sa.Integer(), nullable=True),
        sa.Column("transcript", sa.Text(), nullable=True),
        sa.Column("caller_phone", sa.String(length=32), nullable=True),
        sa.Column("callback_phone", sa.String(length=32), nullable=True),
        sa.Column("phone_source", sa.String(length=32), nullable=True),
        sa.Column("phone_confidence", sa.Float(), nullable=True),
        sa.Column("concern", sa.String(length=64), nullable=True),
        sa.Column("damage_type", sa.String(length=128), nullable=True),
        sa.Column("damage_confidence", sa.Float(), nullable=True),
        sa.Column("classification_reason", sa.Text(), nullable=True),
        sa.Column(
            "status",
            sa.Enum("NEW", "REVIEW", "CALLBACK_REQUESTED", "FAILED", name="mailboxstatus"),
            nullable=False,
        ),
        sa.Column("review_reason", sa.Text(), nullable=True),
        sa.Column("dry_run", sa.Boolean(), nullable=False),
        sa.Column("processing_started_at", sa.DateTime(), nullable=True),
        sa.Column("processed_at", sa.DateTime(), nullable=True),
        sa.Column("submitted_at", sa.DateTime(), nullable=True),
        sa.Column("next_attempt_at", sa.DateTime(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "source_key", name="uq_mailbox_cases_tenant_source_key"),
    )
    op.create_index("ix_mailbox_cases_audio_sha256", "mailbox_cases", ["audio_sha256"])
    op.create_index("ix_mailbox_cases_created_at", "mailbox_cases", ["created_at"])
    op.create_index("ix_mailbox_cases_received_at", "mailbox_cases", ["received_at"])
    op.create_index("ix_mailbox_cases_status", "mailbox_cases", ["status"])
    op.create_index("ix_mailbox_cases_tenant_id", "mailbox_cases", ["tenant_id"])

    op.create_table(
        "mailbox_callback_attempts",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("mailbox_case_id", sa.Integer(), nullable=False),
        sa.Column(
            "status",
            sa.Enum("PREPARED", "SUBMITTED", "FAILED", name="callbackattemptstatus"),
            nullable=False,
        ),
        sa.Column("dry_run", sa.Boolean(), nullable=False),
        sa.Column("form_url", sa.String(length=1024), nullable=False),
        sa.Column("request_data", sa.JSON(), nullable=False),
        sa.Column("result_data", sa.JSON(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["mailbox_case_id"], ["mailbox_cases.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_mailbox_callback_attempts_mailbox_case_id",
        "mailbox_callback_attempts",
        ["mailbox_case_id"],
    )
    op.create_index("ix_mailbox_callback_attempts_status", "mailbox_callback_attempts", ["status"])
    op.create_index("ix_mailbox_callback_attempts_tenant_id", "mailbox_callback_attempts", ["tenant_id"])

    op.create_table(
        "mailbox_case_events",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("mailbox_case_id", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("actor_user_id", sa.Integer(), nullable=True),
        sa.Column("details", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["actor_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["mailbox_case_id"], ["mailbox_cases.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_mailbox_case_events_created_at", "mailbox_case_events", ["created_at"])
    op.create_index("ix_mailbox_case_events_event_type", "mailbox_case_events", ["event_type"])
    op.create_index("ix_mailbox_case_events_mailbox_case_id", "mailbox_case_events", ["mailbox_case_id"])
    op.create_index("ix_mailbox_case_events_tenant_id", "mailbox_case_events", ["tenant_id"])

    op.create_table(
        "mailbox_sync_cursors",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("mailbox_name", sa.String(length=255), nullable=False),
        sa.Column("last_uid", sa.String(length=128), nullable=True),
        sa.Column("last_checked_at", sa.DateTime(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "mailbox_name", name="uq_mailbox_sync_cursor_tenant_name"),
    )
    op.create_index("ix_mailbox_sync_cursors_tenant_id", "mailbox_sync_cursors", ["tenant_id"])


def downgrade():
    op.drop_index("ix_mailbox_sync_cursors_tenant_id", table_name="mailbox_sync_cursors")
    op.drop_table("mailbox_sync_cursors")
    op.drop_index("ix_mailbox_case_events_tenant_id", table_name="mailbox_case_events")
    op.drop_index("ix_mailbox_case_events_mailbox_case_id", table_name="mailbox_case_events")
    op.drop_index("ix_mailbox_case_events_event_type", table_name="mailbox_case_events")
    op.drop_index("ix_mailbox_case_events_created_at", table_name="mailbox_case_events")
    op.drop_table("mailbox_case_events")
    op.drop_index("ix_mailbox_callback_attempts_tenant_id", table_name="mailbox_callback_attempts")
    op.drop_index("ix_mailbox_callback_attempts_status", table_name="mailbox_callback_attempts")
    op.drop_index("ix_mailbox_callback_attempts_mailbox_case_id", table_name="mailbox_callback_attempts")
    op.drop_table("mailbox_callback_attempts")
    op.drop_index("ix_mailbox_cases_tenant_id", table_name="mailbox_cases")
    op.drop_index("ix_mailbox_cases_status", table_name="mailbox_cases")
    op.drop_index("ix_mailbox_cases_received_at", table_name="mailbox_cases")
    op.drop_index("ix_mailbox_cases_created_at", table_name="mailbox_cases")
    op.drop_index("ix_mailbox_cases_audio_sha256", table_name="mailbox_cases")
    op.drop_table("mailbox_cases")
