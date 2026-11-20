"""Zeiterfassung: Urlaubskonto (vacation_entitlements)

Revision ID: fd39707a5bf5
Revises: fabd55a6f4ea
Create Date: 2026-11-20
"""

import sqlalchemy as sa
from alembic import op

revision = "fd39707a5bf5"
down_revision = "fabd55a6f4ea"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "vacation_entitlements",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("year", sa.Integer(), nullable=False),
        sa.Column("days", sa.Numeric(5, 1), nullable=False),
        sa.Column("carryover", sa.Numeric(5, 1), nullable=False),
        sa.Column("updated_by_user_id", sa.Integer(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["updated_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "year", name="uq_vacation_entitlements_user_year"),
    )
    op.create_index("ix_vacation_entitlements_tenant_id", "vacation_entitlements", ["tenant_id"])
    op.create_index("ix_vacation_entitlements_user_id", "vacation_entitlements", ["user_id"])


def downgrade():
    op.drop_index("ix_vacation_entitlements_user_id", table_name="vacation_entitlements")
    op.drop_index("ix_vacation_entitlements_tenant_id", table_name="vacation_entitlements")
    op.drop_table("vacation_entitlements")
