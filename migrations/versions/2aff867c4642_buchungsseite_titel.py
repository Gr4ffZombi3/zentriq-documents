"""Eigener Titel der oeffentlichen Buchungsseite je Buero

Revision ID: 2aff867c4642
Revises: 40e80173a2f1
Create Date: 2026-11-17
"""

import sqlalchemy as sa
from alembic import op

revision = "2aff867c4642"
down_revision = "40e80173a2f1"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("office_settings") as batch:
        batch.add_column(sa.Column("booking_title", sa.String(length=120), nullable=True))


def downgrade():
    with op.batch_alter_table("office_settings") as batch:
        batch.drop_column("booking_title")
