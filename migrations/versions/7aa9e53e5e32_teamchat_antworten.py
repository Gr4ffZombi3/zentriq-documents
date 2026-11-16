"""Teamchat: Antwort auf eine bestimmte Nachricht (reply_to_id)

Revision ID: 7aa9e53e5e32
Revises: dfd7b7c2b552
Create Date: 2026-11-16

Bewusst ohne Fremdschluessel: Absender und Vorschau werden beim Lesen aus der Originalnachricht
abgeleitet; fehlt sie, zeigt der Chat "Originalnachricht nicht verfuegbar". Bestehende Nachrichten
bleiben unveraendert (NULL = keine Antwort).
"""

import sqlalchemy as sa
from alembic import op

revision = "7aa9e53e5e32"
down_revision = "dfd7b7c2b552"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("team_chat_messages") as batch:
        batch.add_column(sa.Column("reply_to_id", sa.Integer(), nullable=True))
        batch.create_index("ix_team_chat_messages_reply_to_id", ["reply_to_id"])


def downgrade():
    with op.batch_alter_table("team_chat_messages") as batch:
        batch.drop_index("ix_team_chat_messages_reply_to_id")
        batch.drop_column("reply_to_id")
