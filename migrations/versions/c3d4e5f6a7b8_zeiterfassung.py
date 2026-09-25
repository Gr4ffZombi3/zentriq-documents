"""Zeiterfassung: Profile, Buchungen, Pausen, Korrekturantraege, Korrekturprotokoll

Revision ID: c3d4e5f6a7b8
Revises: b1c2d3e4f5a6
Create Date: 2026-09-25 21:30:00.000000

"""

import sqlalchemy as sa
from alembic import op

revision = "c3d4e5f6a7b8"
down_revision = "b1c2d3e4f5a6"
branch_labels = None
depends_on = None

OLD_AUDIT_VALUES = (
    "LOGIN_SUCCESS",
    "LOGIN_FAILED",
    "LOGOUT",
    "PASSWORD_RESET_REQUESTED",
    "PASSWORD_RESET_COMPLETED",
)
NEW_AUDIT_VALUES = OLD_AUDIT_VALUES + (
    "USER_CREATED",
    "USER_UPDATED",
    "TIME_CORRECTED",
    "TIME_CORRECTION_REQUESTED",
    "TIME_CORRECTION_DECIDED",
)


def upgrade():
    op.create_table(
        "employee_profiles",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("display_name", sa.String(length=120), nullable=True),
        sa.Column("personnel_number", sa.String(length=50), nullable=True),
        sa.Column("weekly_target_minutes", sa.Integer(), nullable=False),
        sa.Column("workdays", sa.String(length=7), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id"),
    )
    with op.batch_alter_table("employee_profiles", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_employee_profiles_tenant_id"), ["tenant_id"], unique=False)

    op.create_table(
        "work_sessions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("work_date", sa.Date(), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("ended_at", sa.DateTime(), nullable=True),
        sa.Column("open_marker", sa.SmallInteger(), nullable=True),
        sa.Column("source", sa.Enum("STAMP", "MANUAL", name="timeentrysource"), nullable=False),
        sa.Column("is_corrected", sa.Boolean(), nullable=False),
        sa.Column("voided_at", sa.DateTime(), nullable=True),
        sa.Column("voided_by_user_id", sa.Integer(), nullable=True),
        sa.Column("created_by_user_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["voided_by_user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "open_marker", name="uq_work_sessions_user_open"),
    )
    with op.batch_alter_table("work_sessions", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_work_sessions_tenant_id"), ["tenant_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_work_sessions_user_id"), ["user_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_work_sessions_work_date"), ["work_date"], unique=False)

    op.create_table(
        "work_breaks",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("work_session_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("ended_at", sa.DateTime(), nullable=True),
        sa.Column("open_marker", sa.SmallInteger(), nullable=True),
        sa.Column("is_corrected", sa.Boolean(), nullable=False),
        sa.Column("voided_at", sa.DateTime(), nullable=True),
        sa.Column("voided_by_user_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["voided_by_user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["work_session_id"], ["work_sessions.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "open_marker", name="uq_work_breaks_user_open"),
    )
    with op.batch_alter_table("work_breaks", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_work_breaks_tenant_id"), ["tenant_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_work_breaks_user_id"), ["user_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_work_breaks_work_session_id"), ["work_session_id"], unique=False)

    op.create_table(
        "time_correction_requests",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("work_session_id", sa.Integer(), nullable=True),
        sa.Column("work_date", sa.Date(), nullable=False),
        sa.Column("original_started_at", sa.DateTime(), nullable=True),
        sa.Column("original_ended_at", sa.DateTime(), nullable=True),
        sa.Column("requested_started_at", sa.DateTime(), nullable=False),
        sa.Column("requested_ended_at", sa.DateTime(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column(
            "status",
            sa.Enum("PENDING", "APPROVED", "REJECTED", name="correctionrequeststatus"),
            nullable=False,
        ),
        sa.Column("decided_by_user_id", sa.Integer(), nullable=True),
        sa.Column("decided_at", sa.DateTime(), nullable=True),
        sa.Column("decision_note", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["decided_by_user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["work_session_id"], ["work_sessions.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("time_correction_requests", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_time_correction_requests_created_at"), ["created_at"], unique=False)
        batch_op.create_index(batch_op.f("ix_time_correction_requests_status"), ["status"], unique=False)
        batch_op.create_index(batch_op.f("ix_time_correction_requests_tenant_id"), ["tenant_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_time_correction_requests_user_id"), ["user_id"], unique=False)
        batch_op.create_index(
            batch_op.f("ix_time_correction_requests_work_session_id"), ["work_session_id"], unique=False
        )

    op.create_table(
        "time_corrections",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("target_type", sa.String(length=16), nullable=False),
        sa.Column("target_id", sa.Integer(), nullable=False),
        sa.Column("action", sa.String(length=16), nullable=False),
        sa.Column("field", sa.String(length=32), nullable=True),
        sa.Column("old_value", sa.String(length=64), nullable=True),
        sa.Column("new_value", sa.String(length=64), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("request_id", sa.Integer(), nullable=True),
        sa.Column("corrected_by_user_id", sa.Integer(), nullable=True),
        sa.Column("corrected_by_email_snapshot", sa.String(length=255), nullable=True),
        sa.Column("ip_address", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["corrected_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["request_id"], ["time_correction_requests.id"]),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("time_corrections", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_time_corrections_created_at"), ["created_at"], unique=False)
        batch_op.create_index(batch_op.f("ix_time_corrections_tenant_id"), ["tenant_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_time_corrections_user_id"), ["user_id"], unique=False)

    with op.batch_alter_table("audit_logs", schema=None) as batch_op:
        batch_op.alter_column(
            "event_type",
            existing_type=sa.Enum(*OLD_AUDIT_VALUES, name="auditeventtype"),
            type_=sa.Enum(*NEW_AUDIT_VALUES, name="auditeventtype"),
            existing_nullable=False,
        )


def downgrade():
    # Schlaegt bewusst fehl, solange Audit-Eintraege mit den neuen Event-Typen existieren
    # (Audit-Eintraege werden nicht geloescht).
    with op.batch_alter_table("audit_logs", schema=None) as batch_op:
        batch_op.alter_column(
            "event_type",
            existing_type=sa.Enum(*NEW_AUDIT_VALUES, name="auditeventtype"),
            type_=sa.Enum(*OLD_AUDIT_VALUES, name="auditeventtype"),
            existing_nullable=False,
        )
    op.drop_table("time_corrections")
    op.drop_table("time_correction_requests")
    op.drop_table("work_breaks")
    op.drop_table("work_sessions")
    op.drop_table("employee_profiles")
