"""Buchungslinks loeschen koennen (deleted_at)

Revision ID: fb904c644e1e
Revises: 2aff867c4642
Create Date: 2026-11-17
"""

import sqlalchemy as sa
from alembic import op

revision = "fb904c644e1e"
down_revision = "2aff867c4642"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("booking_links") as batch:
        batch.add_column(sa.Column("deleted_at", sa.DateTime(), nullable=True))


def downgrade():
    with op.batch_alter_table("booking_links") as batch:
        batch.drop_column("deleted_at")
