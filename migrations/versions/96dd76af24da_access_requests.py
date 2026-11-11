"""Anfragen von der Willkommensseite (neues Buero / Demo)

Revision ID: 96dd76af24da
Revises: e19e70ebe2a9
Create Date: 2026-11-11 12:00:00.000000

Rein additiv: neue Tabelle access_requests.
"""

import sqlalchemy as sa
from alembic import op

revision = "96dd76af24da"
down_revision = "e19e70ebe2a9"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "access_requests",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=10), nullable=False),
        sa.Column("office_name", sa.String(length=200), nullable=False),
        sa.Column("contact_name", sa.String(length=200), nullable=False),
        sa.Column("email", sa.String(length=255), nullable=False),
        sa.Column("phone", sa.String(length=50), nullable=True),
        sa.Column("message", sa.Text(), nullable=True),
        sa.Column("ip_address", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_access_requests_created_at", "access_requests", ["created_at"])


def downgrade():
    op.drop_index("ix_access_requests_created_at", table_name="access_requests")
    op.drop_table("access_requests")
