"""Buero-Einstellungen: Profil und Terminplanung

Revision ID: c9d0e1f2a3b4
Revises: b8c9d0e1f2a3
"""

import sqlalchemy as sa
from alembic import op

revision = "c9d0e1f2a3b4"
down_revision = "b8c9d0e1f2a3"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "office_settings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("office_name", sa.String(200), nullable=True),
        sa.Column("email", sa.String(255), nullable=True),
        sa.Column("phone", sa.String(50), nullable=True),
        sa.Column("address", sa.String(255), nullable=True),
        sa.Column("website", sa.String(255), nullable=True),
        sa.Column("signature", sa.Text(), nullable=True),
        sa.Column("hours", sa.JSON(), nullable=True),
        sa.Column("default_minutes", sa.Integer(), nullable=False, server_default="30"),
        sa.Column("buffer_minutes", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("lead_minutes", sa.Integer(), nullable=False, server_default="120"),
        sa.Column("horizon_days", sa.Integer(), nullable=False, server_default="14"),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("updated_by_user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.UniqueConstraint("tenant_id", name="uq_office_settings_tenant"),
    )
    op.create_index("ix_office_settings_tenant_id", "office_settings", ["tenant_id"])


def downgrade():
    op.drop_table("office_settings")
