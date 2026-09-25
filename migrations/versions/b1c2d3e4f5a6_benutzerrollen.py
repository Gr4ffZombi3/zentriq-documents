"""Benutzerrollen (ADMIN / MITARBEITER)

Revision ID: b1c2d3e4f5a6
Revises: a9c3e5f7b1d2
Create Date: 2026-09-25 21:00:00.000000

"""

import sqlalchemy as sa
from alembic import op

revision = "b1c2d3e4f5a6"
down_revision = "a9c3e5f7b1d2"
branch_labels = None
depends_on = None

ROLE_ENUM = sa.Enum("ADMIN", "MITARBEITER", name="userrole")


def upgrade():
    with op.batch_alter_table("users", schema=None) as batch_op:
        batch_op.add_column(sa.Column("role", ROLE_ENUM, nullable=False, server_default="MITARBEITER"))

    # Alle bis hierhin existierenden Konten sind Gruender ihres jeweiligen Mandanten und hatten
    # bisher uneingeschraenkten Zugriff - sie werden Admin, damit keine Berechtigung verloren geht.
    # Neue Konten erhalten ihre Rolle danach ausdruecklich (Admin-Seite, CLI, Registrierung).
    op.execute("UPDATE users SET role = 'ADMIN'")


def downgrade():
    with op.batch_alter_table("users", schema=None) as batch_op:
        batch_op.drop_column("role")
