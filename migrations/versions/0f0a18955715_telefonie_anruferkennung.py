"""Telefonie: Anruferkennung ueber Webex Calling (Durchwahl-Zuordnung, eingehende Anrufe)

Revision ID: 0f0a18955715
Revises: fcab228a78d4
Create Date: 2026-11-11 16:00:00.000000

Rein additiv: zwei neue Tabellen, bestehende Daten bleiben unberuehrt.
"""

import sqlalchemy as sa
from alembic import op

revision = "0f0a18955715"
down_revision = "fcab228a78d4"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "telephony_lines",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("webex_person_uuid", sa.String(64), nullable=False),
        sa.Column("label", sa.String(60), nullable=True),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("user_id", name="uq_telephony_lines_user_id"),
        sa.UniqueConstraint("webex_person_uuid", name="uq_telephony_lines_webex_person_uuid"),
    )
    op.create_index("ix_telephony_lines_tenant_id", "telephony_lines", ["tenant_id"])
    op.create_table(
        "telephony_calls",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("call_key", sa.String(64), nullable=False),
        sa.Column("line_id", sa.Integer(), sa.ForeignKey("telephony_lines.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("remote_number", sa.String(50), nullable=True),
        sa.Column("privacy", sa.Boolean(), nullable=False),
        sa.Column("call_type", sa.String(20), nullable=True),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("ended_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("call_key", name="uq_telephony_calls_call_key"),
    )
    op.create_index("ix_telephony_calls_tenant_id", "telephony_calls", ["tenant_id"])
    op.create_index("ix_telephony_calls_line_id", "telephony_calls", ["line_id"])
    op.create_index("ix_telephony_calls_user_created", "telephony_calls", ["user_id", "created_at"])


def downgrade():
    op.drop_table("telephony_calls")
    op.drop_table("telephony_lines")
