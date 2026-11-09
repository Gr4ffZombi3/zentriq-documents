"""Buchungslinks: Freigabe fuer einzelne Mitarbeiter

Revision ID: 1a987a9e7154
Revises: a6c1e7d2b9f4
Create Date: 2026-11-09 21:00:00.000000

Rein additiv: users.can_send_booking_links (Standard: aus). Bestehende Daten unveraendert.
"""

import sqlalchemy as sa
from alembic import op

revision = "1a987a9e7154"
down_revision = "a6c1e7d2b9f4"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("users") as batch:
        batch.add_column(sa.Column("can_send_booking_links", sa.Boolean(), nullable=False, server_default=sa.false()))


def downgrade():
    with op.batch_alter_table("users") as batch:
        batch.drop_column("can_send_booking_links")
