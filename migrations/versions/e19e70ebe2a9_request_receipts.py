"""Quittungen fuer wiederholte Speicher-Anfragen (Idempotenz-Schluessel)

Revision ID: e19e70ebe2a9
Revises: b2049b67a125
Create Date: 2026-11-10 23:30:00.000000

Rein additiv: neue Tabelle request_receipts.
"""

import sqlalchemy as sa
from alembic import op

revision = "e19e70ebe2a9"
down_revision = "b2049b67a125"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "request_receipts",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("method", sa.String(length=8), nullable=False),
        sa.Column("path", sa.String(length=255), nullable=False),
        sa.Column("state", sa.String(length=10), nullable=False),
        sa.Column("status_code", sa.Integer(), nullable=True),
        sa.Column("content_type", sa.String(length=100), nullable=True),
        sa.Column("location", sa.String(length=500), nullable=True),
        sa.Column("body", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "key", name="uq_request_receipts_user_key"),
    )
    op.create_index("ix_request_receipts_created_at", "request_receipts", ["created_at"])


def downgrade():
    op.drop_index("ix_request_receipts_created_at", table_name="request_receipts")
    op.drop_table("request_receipts")
