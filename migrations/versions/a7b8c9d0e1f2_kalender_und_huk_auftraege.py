"""Gemeinsamer Buerokalender und HUK-Kfz-Beitragsberechnungen

Revision ID: a7b8c9d0e1f2
Revises: f2a3b4c5d6e7
Create Date: 2026-11-06 21:00:00
"""

import sqlalchemy as sa
from alembic import op

revision = "a7b8c9d0e1f2"
down_revision = "f2a3b4c5d6e7"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "calendar_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("starts_at", sa.DateTime(), nullable=False),
        sa.Column("ends_at", sa.DateTime(), nullable=False),
        sa.Column("assignee_user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("customer_id", sa.Integer(), sa.ForeignKey("customers.id", ondelete="SET NULL"), nullable=True),
        sa.Column("location_kind", sa.String(20), nullable=True),
        sa.Column("location", sa.String(255), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="scheduled"),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("client_ref", sa.String(64), nullable=True),
        sa.Column("created_by_user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_by_user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("tenant_id", "created_by_user_id", "client_ref", name="uq_calendar_events_client_ref"),
    )
    op.create_index("ix_calendar_events_tenant_id", "calendar_events", ["tenant_id"])
    op.create_index("ix_calendar_events_customer_id", "calendar_events", ["customer_id"])
    op.create_index("ix_calendar_events_updated_at", "calendar_events", ["updated_at"])
    op.create_index("ix_calendar_events_tenant_range", "calendar_events", ["tenant_id", "starts_at", "ends_at"])
    op.create_index("ix_calendar_events_assignee_range", "calendar_events", ["assignee_user_id", "starts_at"])

    op.create_table(
        "calendar_event_changes",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("event_id", sa.Integer(), nullable=False),
        sa.Column("action", sa.String(20), nullable=False),
        sa.Column("changes", sa.JSON(), nullable=True),
        sa.Column("actor_user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("actor_email_snapshot", sa.String(255), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_calendar_event_changes_tenant_id", "calendar_event_changes", ["tenant_id"])
    op.create_index("ix_calendar_event_changes_event_id", "calendar_event_changes", ["event_id"])

    op.create_table(
        "huk_quote_jobs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="draft"),
        sa.Column("customer_id", sa.Integer(), sa.ForeignKey("customers.id", ondelete="SET NULL"), nullable=True),
        sa.Column("input_data", sa.JSON(), nullable=True),
        sa.Column("confirmed_at", sa.DateTime(), nullable=True),
        sa.Column("confirmed_by_user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("result", sa.JSON(), nullable=True),
        sa.Column("message", sa.String(500), nullable=True),
        sa.Column("error_code", sa.String(40), nullable=True),
        sa.Column("celery_task_id", sa.String(64), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cancel_requested", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("ai_calls", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("ai_cost_usd", sa.Numeric(10, 4), nullable=False, server_default="0"),
        sa.Column("evidence_path", sa.String(255), nullable=True),
        sa.Column("client_ref", sa.String(64), nullable=True),
        sa.Column("created_by_user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("tenant_id", "created_by_user_id", "client_ref", name="uq_huk_quote_jobs_client_ref"),
    )
    op.create_index("ix_huk_quote_jobs_tenant_id", "huk_quote_jobs", ["tenant_id"])
    op.create_index("ix_huk_quote_jobs_customer_id", "huk_quote_jobs", ["customer_id"])
    op.create_index("ix_huk_quote_jobs_created_by_user_id", "huk_quote_jobs", ["created_by_user_id"])
    op.create_index("ix_huk_quote_jobs_tenant_status", "huk_quote_jobs", ["tenant_id", "status"])


def downgrade():
    op.drop_table("huk_quote_jobs")
    op.drop_table("calendar_event_changes")
    op.drop_table("calendar_events")
