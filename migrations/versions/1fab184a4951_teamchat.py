"""Interner Teamchat (1:1): team_chats, team_chat_messages

Revision ID: 1fab184a4951
Revises: f2c95a5229d9
Create Date: 2026-11-12 14:00:00.000000

Rein additiv: zwei neue Tabellen. Bestehende Daten werden nicht veraendert.
"""

import sqlalchemy as sa
from alembic import op

revision = "1fab184a4951"
down_revision = "f2c95a5229d9"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "team_chats",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("user_low_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_high_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("low_read_id", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("high_read_id", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_message_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("tenant_id", "user_low_id", "user_high_id", name="uq_team_chats_pair"),
    )
    op.create_index("ix_team_chats_tenant_id", "team_chats", ["tenant_id"])
    op.create_index("ix_team_chats_user_low_id", "team_chats", ["user_low_id"])
    op.create_index("ix_team_chats_user_high_id", "team_chats", ["user_high_id"])
    op.create_table(
        "team_chat_messages",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("chat_id", sa.Integer(), sa.ForeignKey("team_chats.id", ondelete="CASCADE"), nullable=False),
        sa.Column("sender_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("client_ref", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("sender_id", "client_ref", name="uq_team_chat_messages_client_ref"),
    )
    op.create_index("ix_team_chat_messages_tenant_id", "team_chat_messages", ["tenant_id"])
    op.create_index("ix_team_chat_messages_chat_id_id", "team_chat_messages", ["chat_id", "id"])


def downgrade():
    op.drop_index("ix_team_chat_messages_chat_id_id", table_name="team_chat_messages")
    op.drop_index("ix_team_chat_messages_tenant_id", table_name="team_chat_messages")
    op.drop_table("team_chat_messages")
    op.drop_index("ix_team_chats_user_high_id", table_name="team_chats")
    op.drop_index("ix_team_chats_user_low_id", table_name="team_chats")
    op.drop_index("ix_team_chats_tenant_id", table_name="team_chats")
    op.drop_table("team_chats")
