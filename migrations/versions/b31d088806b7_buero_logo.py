"""Buerologo fuer die oeffentliche Buchungsseite

Revision ID: b31d088806b7
Revises: b614852f0937
Create Date: 2026-11-17
"""

import sqlalchemy as sa
from alembic import op

revision = "b31d088806b7"
down_revision = "b614852f0937"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("office_settings") as batch:
        # MariaDB/MySQL: Laenge > 64 KB ergibt MEDIUMBLOB (bis 16 MB); das Logo selbst ist < 1 MB.
        batch.add_column(sa.Column("logo_png", sa.LargeBinary(length=16 * 1024 * 1024 - 1), nullable=True))
        batch.add_column(sa.Column("logo_rev", sa.String(length=16), nullable=True))
        batch.create_index("ix_office_settings_logo_rev", ["logo_rev"])


def downgrade():
    with op.batch_alter_table("office_settings") as batch:
        batch.drop_index("ix_office_settings_logo_rev")
        batch.drop_column("logo_rev")
        batch.drop_column("logo_png")
