"""Vorgaenge ohne Kundenakte (Notizen/Termine, spaetere Zuordnung beim Listenimport)

Revision ID: b2049b67a125
Revises: b7c8d9e0f1a2
Create Date: 2026-11-10 22:30:00.000000

Rein additiv: zwei neue Tabellen und eine leere Spalte calendar_events.pending_case_id.
"""

import sqlalchemy as sa
from alembic import op

revision = "b2049b67a125"
down_revision = "b7c8d9e0f1a2"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "pending_cases",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id"), nullable=False, index=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("name_key", sa.String(255), nullable=True),
        sa.Column("vn_number", sa.String(20), nullable=True),
        sa.Column("vn_key", sa.String(12), nullable=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="open"),
        sa.Column("customer_id", sa.Integer(), sa.ForeignKey("customers.id", ondelete="SET NULL"), nullable=True, index=True),
        sa.Column("link_basis", sa.String(20), nullable=True),
        sa.Column("linked_at", sa.DateTime(), nullable=True),
        sa.Column("linked_by_user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_by_user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_pending_cases_tenant_status", "pending_cases", ["tenant_id", "status"])
    op.create_index("ix_pending_cases_tenant_vn_key", "pending_cases", ["tenant_id", "vn_key"])
    op.create_index("ix_pending_cases_tenant_name_key", "pending_cases", ["tenant_id", "name_key"])
    op.create_table(
        "pending_case_notes",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id"), nullable=False, index=True),
        sa.Column("case_id", sa.Integer(), sa.ForeignKey("pending_cases.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("created_by_user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("client_ref", sa.String(64), nullable=True),
        sa.Column("customer_note_id", sa.Integer(), sa.ForeignKey("customer_notes.id", ondelete="SET NULL"), nullable=True),
        sa.UniqueConstraint("tenant_id", "created_by_user_id", "client_ref", name="uq_pending_case_notes_client_ref"),
    )
    with op.batch_alter_table("calendar_events") as batch:
        batch.add_column(sa.Column("pending_case_id", sa.Integer(), nullable=True))
        batch.create_index("ix_calendar_events_pending_case_id", ["pending_case_id"])
        batch.create_foreign_key("fk_calendar_events_pending_case_id", "pending_cases", ["pending_case_id"], ["id"], ondelete="SET NULL")


def downgrade():
    with op.batch_alter_table("calendar_events") as batch:
        batch.drop_constraint("fk_calendar_events_pending_case_id", type_="foreignkey")
        batch.drop_index("ix_calendar_events_pending_case_id")
        batch.drop_column("pending_case_id")
    op.drop_table("pending_case_notes")
    op.drop_index("ix_pending_cases_tenant_name_key", table_name="pending_cases")
    op.drop_index("ix_pending_cases_tenant_vn_key", table_name="pending_cases")
    op.drop_index("ix_pending_cases_tenant_status", table_name="pending_cases")
    op.drop_table("pending_cases")
