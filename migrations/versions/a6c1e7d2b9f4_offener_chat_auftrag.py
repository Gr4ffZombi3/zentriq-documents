"""Offener Auftrag im ZentriqAI-Chat (Rueckfrage mit bereits geklaerten Angaben)

Revision ID: a6c1e7d2b9f4
Revises: 5fb9bd9d93c1
"""

import sqlalchemy as sa
from alembic import op

revision = "a6c1e7d2b9f4"
down_revision = "5fb9bd9d93c1"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("ai_conversations") as batch:
        batch.add_column(sa.Column("pending_task", sa.JSON(), nullable=True))


def downgrade():
    with op.batch_alter_table("ai_conversations") as batch:
        batch.drop_column("pending_task")
