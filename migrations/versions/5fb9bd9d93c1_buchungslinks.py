"""Persoenliche Buchungslinks fuer Kunden ohne Anmeldung

Revision ID: 5fb9bd9d93c1
Revises: d0e1f2a3b4c5
"""

import sqlalchemy as sa
from alembic import op

revision = "5fb9bd9d93c1"
down_revision = "d0e1f2a3b4c5"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "booking_links",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("token_enc", sa.Text(), nullable=False),
        sa.Column("title", sa.String(120), nullable=False),
        sa.Column("location_kind", sa.String(20), nullable=True),
        sa.Column("minutes", sa.Integer(), nullable=False),
        sa.Column("assignee_user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("customer_id", sa.Integer(), sa.ForeignKey("customers.id", ondelete="SET NULL"), nullable=True),
        sa.Column("offers", sa.JSON(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("max_bookings", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("booking_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("used_at", sa.DateTime(), nullable=True),
        sa.Column("last_submission", sa.String(64), nullable=True),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
        sa.Column("revoked_by_user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_by_user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_booking_links_tenant_id", "booking_links", ["tenant_id"])
    op.create_index("ix_booking_links_token_hash", "booking_links", ["token_hash"], unique=True)
    op.create_index("ix_booking_links_customer_id", "booking_links", ["customer_id"])


def downgrade():
    # MariaDB: Indizes, die Fremdschluessel stuetzen, verschwinden mit der Tabelle.
    op.drop_table("booking_links")
