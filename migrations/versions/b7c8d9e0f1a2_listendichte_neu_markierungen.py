"""Arbeitsplatz: Listendichte (kompakt/geraeumig) und gelesene "Neu"-Markierungen je Benutzer

Revision ID: b7c8d9e0f1a2
Revises: a9e1c2d3f4b5
Create Date: 2026-11-10 21:00:00.000000

Rein additiv: zwei neue, leere Spalten in user_preferences.
"""

import sqlalchemy as sa
from alembic import op

revision = "b7c8d9e0f1a2"
down_revision = "a9e1c2d3f4b5"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("user_preferences") as batch:
        batch.add_column(sa.Column("list_density", sa.String(12), nullable=True))
        batch.add_column(sa.Column("seen_features", sa.JSON(), nullable=True))


def downgrade():
    with op.batch_alter_table("user_preferences") as batch:
        batch.drop_column("seen_features")
        batch.drop_column("list_density")
