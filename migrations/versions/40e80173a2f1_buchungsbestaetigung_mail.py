"""Bestaetigungs-E-Mail fuer oeffentlich gebuchte Termine (genau eine je Termin)

Revision ID: 40e80173a2f1
Revises: b31d088806b7
Create Date: 2026-11-17
"""

import sqlalchemy as sa
from alembic import op

revision = "40e80173a2f1"
down_revision = "b31d088806b7"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "booking_confirmations",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("event_id", sa.Integer(), sa.ForeignKey("calendar_events.id", ondelete="CASCADE"), nullable=False),
        sa.Column("recipient", sa.String(length=255), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=True),
        sa.Column("phone", sa.String(length=30), nullable=True),
        sa.Column("title", sa.String(length=120), nullable=False),
        sa.Column("status", sa.String(length=10), nullable=False, server_default="pending"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error", sa.String(length=120), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("queued_at", sa.DateTime(), nullable=False),
        sa.Column("sent_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("event_id", name="uq_booking_confirmations_event"),
    )
    op.create_index("ix_booking_confirmations_tenant_id", "booking_confirmations", ["tenant_id"])
    op.create_index("ix_booking_confirmations_status", "booking_confirmations", ["status"])


def downgrade():
    op.drop_index("ix_booking_confirmations_status", table_name="booking_confirmations")
    op.drop_index("ix_booking_confirmations_tenant_id", table_name="booking_confirmations")
    op.drop_table("booking_confirmations")
