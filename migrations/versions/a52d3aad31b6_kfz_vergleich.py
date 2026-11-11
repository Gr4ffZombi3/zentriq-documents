"""Kfz-Vergleich aus Versicherungsschein: Vergleichsdaten am HUK-Berechnungsauftrag

Revision ID: a52d3aad31b6
Revises: 0f0a18955715
Create Date: 2026-11-11
"""

import sqlalchemy as sa
from alembic import op

revision = "a52d3aad31b6"
down_revision = "0f0a18955715"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("huk_quote_jobs") as batch_op:
        batch_op.add_column(sa.Column("comparison", sa.JSON(), nullable=True))


def downgrade():
    with op.batch_alter_table("huk_quote_jobs") as batch_op:
        batch_op.drop_column("comparison")
