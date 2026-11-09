"""Zeiterfassung: Wochenplan je Mitarbeiter mit Gueltigkeitsdatum

Revision ID: 07d7a2b02ec2
Revises: df2d59947081
Create Date: 2026-11-10 10:00:00.000000

Rein additiv: neue Tabelle employee_work_plans. Ohne Plan gilt weiter die bisherige
gleichmaessige Verteilung (employee_profiles.weekly_target_minutes / workdays).
"""

import sqlalchemy as sa
from alembic import op

revision = "07d7a2b02ec2"
down_revision = "df2d59947081"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "employee_work_plans",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id"), nullable=False, index=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("valid_from", sa.Date(), nullable=False),
        sa.Column("days", sa.JSON(), nullable=False),
        sa.Column("created_by_user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("user_id", "valid_from", name="uq_employee_work_plans_user_from"),
    )


def downgrade():
    op.drop_table("employee_work_plans")
