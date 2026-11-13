"""Allgemeiner Buchungslink je Buero: office_booking_pages

Revision ID: dfd7b7c2b552
Revises: 1fab184a4951
Create Date: 2026-11-13 09:00:00.000000

Rein additiv: neue Tabelle. Bestehende Daten (auch persoenliche Buchungslinks) bleiben unveraendert.
"""

import sqlalchemy as sa
from alembic import op

revision = "dfd7b7c2b552"
down_revision = "1fab184a4951"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "office_booking_pages",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("slug", sa.String(40), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("title", sa.String(80), nullable=False),
        sa.Column("minutes", sa.Integer(), nullable=False, server_default="30"),
        sa.Column("assignee_user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("windows", sa.JSON(), nullable=True),
        sa.Column("horizon_days", sa.Integer(), nullable=False, server_default="21"),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("updated_by_user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.UniqueConstraint("tenant_id", name="uq_office_booking_pages_tenant_id"),
        sa.UniqueConstraint("slug", name="uq_office_booking_pages_slug"),
    )
    op.create_index("ix_office_booking_pages_tenant_id", "office_booking_pages", ["tenant_id"])


def downgrade():
    op.drop_index("ix_office_booking_pages_tenant_id", table_name="office_booking_pages")
    op.drop_table("office_booking_pages")
