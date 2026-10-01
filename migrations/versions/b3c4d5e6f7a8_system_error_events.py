"""Technisches Fehlerprotokoll fuer das Plattform-Panel

Revision ID: b3c4d5e6f7a8
Revises: a2b3c4d5e6f7
Create Date: 2026-10-02 09:30:00.000000

Rein additiv: neue Tabelle system_error_events (ohne Fehlertexte/Inhalte).
"""

import sqlalchemy as sa
from alembic import op

revision = "b3c4d5e6f7a8"
down_revision = "a2b3c4d5e6f7"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "system_error_events",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(), nullable=False),
        sa.Column("source", sa.String(length=20), nullable=False),
        sa.Column("location", sa.String(length=255), nullable=True),
        sa.Column("error_type", sa.String(length=120), nullable=False),
        sa.Column("tenant_id", sa.Integer(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("system_error_events", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_system_error_events_occurred_at"), ["occurred_at"], unique=False)


def downgrade():
    with op.batch_alter_table("system_error_events", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_system_error_events_occurred_at"))
    op.drop_table("system_error_events")
