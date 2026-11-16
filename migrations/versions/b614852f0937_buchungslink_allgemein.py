"""Buchungslinks: Art "proposal" (2-5 vorgeschlagene Termine, Einmalbuchung) oder "open" (allgemein:
freie Zeiten live aus dem Kalender, mehrfach buchbar, ohne Ablauf)

Revision ID: b614852f0937
Revises: 7aa9e53e5e32
Create Date: 2026-11-16

Bestehende Links von Mitarbeitern wurden immer aus den Buerozeiten erzeugt (Mitarbeiter koennen keine
eigenen Zeiten waehlen) - sie sind allgemeine Links und werden auf "open" gesetzt, damit bereits
verschickte URLs dauerhaft fuer weitere Kunden funktionieren. Links von Buero-Admins bleiben "proposal".
Es werden keine Daten geloescht.
"""

import sqlalchemy as sa
from alembic import op

revision = "b614852f0937"
down_revision = "7aa9e53e5e32"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("booking_links") as batch:
        batch.add_column(sa.Column("kind", sa.String(length=10), nullable=False, server_default="proposal"))
    links = sa.table("booking_links", sa.column("kind", sa.String), sa.column("created_by_user_id", sa.Integer))
    users = sa.table("users", sa.column("id", sa.Integer), sa.column("role", sa.String))
    employees = sa.select(users.c.id).where(users.c.role.in_(("EMPLOYEE", "employee")))
    op.execute(links.update().where(links.c.created_by_user_id.in_(employees)).values(kind="open"))


def downgrade():
    with op.batch_alter_table("booking_links") as batch:
        batch.drop_column("kind")
