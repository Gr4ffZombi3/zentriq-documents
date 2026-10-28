"""Kundennotizen und offene Rueckrufe (ZentriqAI-Eingabefeld)

Revision ID: e1f2a3b4c5d6
Revises: d9e0f1a2b3c4
Create Date: 2026-10-28 12:00:00.000000

Rein additiv: zwei neue Tabellen customer_contacts (Kontaktvorgaenge) und customer_notes.
Bestehende Tabellen und Daten werden nicht veraendert.
"""

import sqlalchemy as sa
from alembic import op

revision = "e1f2a3b4c5d6"
down_revision = "d9e0f1a2b3c4"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "customer_contacts",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("customer_id", sa.Integer(), nullable=False),
        sa.Column("topic", sa.String(length=200), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("outcome", sa.String(length=30), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("created_by_user_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_by_user_id", sa.Integer(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("closed_by_user_id", sa.Integer(), nullable=True),
        sa.Column("closed_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.ForeignKeyConstraint(["customer_id"], ["customers.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["updated_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["closed_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_customer_contacts_tenant_id", "customer_contacts", ["tenant_id"], unique=False)
    op.create_index("ix_customer_contacts_customer_id", "customer_contacts", ["customer_id"], unique=False)
    op.create_index(
        "ix_customer_contacts_tenant_status_updated", "customer_contacts", ["tenant_id", "status", "updated_at"], unique=False
    )

    op.create_table(
        "customer_notes",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("customer_id", sa.Integer(), nullable=False),
        sa.Column("contact_id", sa.Integer(), nullable=True),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("event", sa.String(length=30), nullable=True),
        sa.Column("created_by_user_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("client_ref", sa.String(length=64), nullable=True),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.ForeignKeyConstraint(["customer_id"], ["customers.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["contact_id"], ["customer_contacts.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "created_by_user_id", "client_ref", name="uq_customer_notes_client_ref"),
    )
    op.create_index("ix_customer_notes_tenant_id", "customer_notes", ["tenant_id"], unique=False)
    op.create_index("ix_customer_notes_contact_id", "customer_notes", ["contact_id"], unique=False)
    op.create_index("ix_customer_notes_created_at", "customer_notes", ["created_at"], unique=False)
    op.create_index("ix_customer_notes_customer_created", "customer_notes", ["customer_id", "created_at"], unique=False)


def downgrade():
    op.drop_table("customer_notes")
    op.drop_table("customer_contacts")
