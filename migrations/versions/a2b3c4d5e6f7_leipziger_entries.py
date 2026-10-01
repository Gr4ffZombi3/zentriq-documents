"""Leipziger Liste: normalisierte Vorgaenge (Suche, Vermittler-Zuordnung, Importvergleich)

Revision ID: a2b3c4d5e6f7
Revises: f7a8b9c0d1e2
Create Date: 2026-10-02 09:00:00.000000

Neue Tabelle leipziger_entries. Bestehende, fertig ausgewertete Listen werden einmalig aus
document_customers.row_data nachgetragen (nur lesend auf den Bestandsdaten, je Dokument
eine Einfuegung). Bestehende Tabellen werden nicht veraendert.
"""

import json

import sqlalchemy as sa
from alembic import op

revision = "a2b3c4d5e6f7"
down_revision = "f7a8b9c0d1e2"
branch_labels = None
depends_on = None


def upgrade():
    entries = op.create_table(
        "leipziger_entries",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("document_id", sa.Integer(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("contract_number", sa.String(length=100), nullable=True),
        sa.Column("contract_key", sa.String(length=100), nullable=True),
        sa.Column("customer_name", sa.String(length=255), nullable=True),
        sa.Column("customer_key", sa.String(length=255), nullable=True),
        sa.Column("broker_number", sa.String(length=50), nullable=True),
        sa.Column("broker_key", sa.String(length=50), nullable=True),
        sa.Column("status_code", sa.String(length=20), nullable=True),
        sa.Column("product_line", sa.String(length=100), nullable=True),
        sa.Column("start_date", sa.String(length=10), nullable=True),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("leipziger_entries", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_leipziger_entries_document_id"), ["document_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_leipziger_entries_tenant_id"), ["tenant_id"], unique=False)
        batch_op.create_index("ix_leipziger_entries_doc_broker", ["tenant_id", "document_id", "broker_key"], unique=False)
        batch_op.create_index("ix_leipziger_entries_doc_contract", ["tenant_id", "document_id", "contract_key"], unique=False)

    _backfill(entries)


def _backfill(entries):
    # Dieselbe Umrechnung wie beim Import (reine Funktion ohne Modellzugriff).
    from app.services.leipziger_entries import entry_values

    connection = op.get_bind()
    documents = connection.execute(
        sa.text("SELECT id, tenant_id FROM documents WHERE doc_type = 'LEIPZIGER_LISTE' AND status = 'DONE'")
    ).all()
    for document_id, tenant_id in documents:
        rows = connection.execute(
            sa.text(
                "SELECT dc.row_data, c.name FROM document_customers dc "
                "LEFT JOIN customers c ON c.id = dc.customer_id WHERE dc.document_id = :document_id"
            ),
            {"document_id": document_id},
        ).all()
        values = []
        for row_data, customer_name in rows:
            if isinstance(row_data, (str, bytes)):
                try:
                    row_data = json.loads(row_data)
                except ValueError:
                    row_data = None
            for row in row_data or []:
                item = entry_values(row, customer_name, len(values))
                if item is not None:
                    values.append({**item, "tenant_id": tenant_id, "document_id": document_id})
        if values:
            op.bulk_insert(entries, values)


def downgrade():
    with op.batch_alter_table("leipziger_entries", schema=None) as batch_op:
        batch_op.drop_index("ix_leipziger_entries_doc_contract")
        batch_op.drop_index("ix_leipziger_entries_doc_broker")
        batch_op.drop_index(batch_op.f("ix_leipziger_entries_tenant_id"))
        batch_op.drop_index(batch_op.f("ix_leipziger_entries_document_id"))
    op.drop_table("leipziger_entries")
