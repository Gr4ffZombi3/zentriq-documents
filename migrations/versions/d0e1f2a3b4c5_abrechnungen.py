"""Abrechnungen: eingereichte Belege und erkannte Buchungen

Revision ID: d0e1f2a3b4c5
Revises: c9d0e1f2a3b4
"""

import sqlalchemy as sa
from alembic import op

revision = "d0e1f2a3b4c5"
down_revision = "c9d0e1f2a3b4"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "settlements",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="processing"),
        sa.Column("original_filename", sa.String(255), nullable=False),
        sa.Column("stored_path", sa.String(255), nullable=False),
        sa.Column("file_sha256", sa.String(64), nullable=False),
        sa.Column("content_fingerprint", sa.String(64), nullable=True),
        sa.Column("duplicate_of_id", sa.Integer(), sa.ForeignKey("settlements.id", ondelete="SET NULL"), nullable=True),
        sa.Column("period_start", sa.Date(), nullable=True),
        sa.Column("period_end", sa.Date(), nullable=True),
        sa.Column("broker_number", sa.String(50), nullable=True),
        sa.Column("employee_user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("text_method", sa.String(20), nullable=True),
        sa.Column("page_count", sa.Integer(), nullable=True),
        sa.Column("message", sa.String(500), nullable=True),
        sa.Column("uploaded_by_user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("processed_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("tenant_id", "file_sha256", name="uq_settlements_file"),
    )
    for col in ("tenant_id", "content_fingerprint", "period_start", "employee_user_id", "uploaded_by_user_id"):
        op.create_index(f"ix_settlements_{col}", "settlements", [col])
    op.create_table(
        "settlement_lines",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("settlement_id", sa.Integer(), sa.ForeignKey("settlements.id", ondelete="CASCADE"), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("page", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("raw_text", sa.String(500), nullable=False),
        sa.Column("booking_date", sa.Date(), nullable=True),
        sa.Column("contract_ref", sa.String(60), nullable=True),
        sa.Column("sparte", sa.String(40), nullable=True),
        sa.Column("booking_type", sa.String(20), nullable=True),
        sa.Column("booking_label", sa.String(120), nullable=True),
        sa.Column("amount", sa.Numeric(12, 2), nullable=True),
        sa.Column("is_ten", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("needs_review", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("review_reason", sa.String(200), nullable=True),
        sa.Column("review_state", sa.String(20), nullable=True),
        sa.Column("reviewed_by_user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(), nullable=True),
    )
    for col in ("tenant_id", "settlement_id", "contract_ref"):
        op.create_index(f"ix_settlement_lines_{col}", "settlement_lines", [col])


def downgrade():
    op.drop_table("settlement_lines")
    op.drop_table("settlements")
