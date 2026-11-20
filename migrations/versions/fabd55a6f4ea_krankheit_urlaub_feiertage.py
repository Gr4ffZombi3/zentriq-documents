"""Zeiterfassung: Krankheit/Urlaub (time_absences) und Bundesland fuer Feiertage

Revision ID: fabd55a6f4ea
Revises: fb904c644e1e
Create Date: 2026-11-20
"""

import sqlalchemy as sa
from alembic import op

revision = "fabd55a6f4ea"
down_revision = "fb904c644e1e"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "time_absences",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("start_date", sa.Date(), nullable=False),
        sa.Column("end_date", sa.Date(), nullable=False),
        sa.Column("refund_status", sa.String(length=16), nullable=True),
        sa.Column("created_by_user_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_by_user_id", sa.Integer(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("deleted_at", sa.DateTime(), nullable=True),
        sa.Column("deleted_by_user_id", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["updated_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["deleted_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_time_absences_tenant_id", "time_absences", ["tenant_id"])
    op.create_index("ix_time_absences_user_start", "time_absences", ["user_id", "start_date"])
    with op.batch_alter_table("office_settings") as batch:
        batch.add_column(sa.Column("holiday_state", sa.String(length=2), nullable=False, server_default="NW"))


def downgrade():
    with op.batch_alter_table("office_settings") as batch:
        batch.drop_column("holiday_state")
    op.drop_index("ix_time_absences_user_start", table_name="time_absences")
    op.drop_index("ix_time_absences_tenant_id", table_name="time_absences")
    op.drop_table("time_absences")
