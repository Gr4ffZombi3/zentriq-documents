"""Zentriq-Kundenstamm: Memos je Kunde, Datenherkunft, Vermittlernummer, Vorgang -> Kunde

Revision ID: d6e7f8a9b0c1
Revises: c4d5e6f7a8b9
Create Date: 2026-10-02 18:00:00.000000

Rein additiv:
- neue Tabelle customer_memos (einem Kunden zugeordnete Memo-Transkripte),
- neue nullable Spalten customers.broker_number/source/field_sources,
- neue nullable Spalte leipziger_entries.customer_id mit Index (je Mandant),
- neue Enum-Werte MEMO_ASSIGNED, CUSTOMER_CREATED, ASSISTANT_USED fuer das Audit-Log.

Neuberechnung des abgeleiteten Vergleichsschluessels customers.name_key (korrigierte
Umlaut-Normalisierung, siehe _recompute_name_keys).

Einmaliges Befuellen NUR der neuen Spalten aus vorhandenen Daten: leipziger_entries.customer_id
aus den gespeicherten Listenzeilen (document_customers.row_data), danach je Kunde die
Vermittlernummer des neuesten Vorgangs und die Herkunft LEIPZIGER_LISTE. Bestehende Werte
werden nicht veraendert, nichts wird geloescht.
"""

from collections import defaultdict

import sqlalchemy as sa
from alembic import op

revision = "d6e7f8a9b0c1"
down_revision = "c4d5e6f7a8b9"
branch_labels = None
depends_on = None

OLD_AUDIT_VALUES = (
    "LOGIN_SUCCESS",
    "LOGIN_FAILED",
    "LOGOUT",
    "PASSWORD_RESET_REQUESTED",
    "PASSWORD_RESET_COMPLETED",
    "USER_CREATED",
    "USER_UPDATED",
    "TIME_CORRECTED",
    "TIME_CORRECTION_REQUESTED",
    "TIME_CORRECTION_DECIDED",
    "USER_DELETED",
    "PASSWORD_CHANGED",
    "PASSWORD_RESET_TRIGGERED",
    "TWO_FACTOR_ENABLED",
    "TWO_FACTOR_RESET",
    "TWO_FACTOR_FAILED",
    "RECOVERY_CODE_USED",
    "RECOVERY_CODES_REGENERATED",
    "TENANT_CREATED",
    "TENANT_UPDATED",
    "TIME_CLOCK_IN",
    "TIME_CLOCK_OUT",
    "MEMO_TRANSCRIBED",
    "LEIPZIGER_LIST_UPLOADED",
    "SESSIONS_REVOKED",
    "CUSTOMER_MERGED",
)
NEW_AUDIT_VALUES = OLD_AUDIT_VALUES + ("MEMO_ASSIGNED", "CUSTOMER_CREATED", "ASSISTANT_USED")


def _alter_audit_enum(from_values, to_values):
    with op.batch_alter_table("audit_logs", schema=None) as batch_op:
        batch_op.alter_column(
            "event_type",
            existing_type=sa.Enum(*from_values, name="auditeventtype"),
            type_=sa.Enum(*to_values, name="auditeventtype"),
            existing_nullable=False,
        )


def upgrade():
    op.create_table(
        "customer_memos",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("customer_id", sa.Integer(), nullable=False),
        sa.Column("transcript", sa.Text(), nullable=False),
        sa.Column("transcript_sha256", sa.String(length=64), nullable=False),
        sa.Column("matched_by", sa.String(length=20), nullable=False),
        sa.Column("created_by_user_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.ForeignKeyConstraint(["customer_id"], ["customers.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "customer_id", "transcript_sha256", name="uq_customer_memos_transcript"),
    )
    with op.batch_alter_table("customer_memos", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_customer_memos_tenant_id"), ["tenant_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_customer_memos_customer_id"), ["customer_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_customer_memos_created_at"), ["created_at"], unique=False)

    with op.batch_alter_table("customers", schema=None) as batch_op:
        batch_op.add_column(sa.Column("broker_number", sa.String(length=50), nullable=True))
        batch_op.add_column(sa.Column("source", sa.String(length=20), nullable=True))
        batch_op.add_column(sa.Column("field_sources", sa.JSON(), nullable=True))

    with op.batch_alter_table("leipziger_entries", schema=None) as batch_op:
        batch_op.add_column(sa.Column("customer_id", sa.Integer(), nullable=True))
        batch_op.create_foreign_key(
            "fk_leipziger_entries_customer_id", "customers", ["customer_id"], ["id"], ondelete="SET NULL"
        )
        batch_op.create_index("ix_leipziger_entries_customer", ["tenant_id", "customer_id"], unique=False)

    _alter_audit_enum(OLD_AUDIT_VALUES, NEW_AUDIT_VALUES)
    _recompute_name_keys()
    _backfill_entry_customers()
    _backfill_customer_fields()


def _recompute_name_keys():
    """customers.name_key ist ein reiner, abgeleiteter Vergleichsschluessel. Er wurde bisher mit
    fehlerhafter Umlaut-Ersetzung berechnet ("Müller" -> "muller", "Mueller" -> "mueller") und
    wird hier mit der korrigierten Normalisierung neu berechnet - nur wo er sich aendert."""
    from app.utils.customer_keys import customer_keys

    connection = op.get_bind()
    customers = sa.table("customers", sa.column("id", sa.Integer), sa.column("name_key", sa.String))
    for customer_id, name, stored in connection.execute(sa.text("SELECT id, name, name_key FROM customers")).all():
        key = customer_keys(name, None, None)["name_key"]
        if key != stored:
            connection.execute(customers.update().where(customers.c.id == customer_id).values(name_key=key))


def _backfill_entry_customers():
    """Ordnet vorhandene Vorgaenge ihrem Kunden zu - mit derselben Berechnung wie beim Import
    (app/services/leipziger_entries.entry_values). Nur eindeutige Treffer werden gesetzt."""
    import json

    from app.services.leipziger_entries import entry_values

    connection = op.get_bind()
    entries = sa.table("leipziger_entries", sa.column("id", sa.Integer), sa.column("customer_id", sa.Integer))
    document_ids = [row[0] for row in connection.execute(sa.text("SELECT DISTINCT document_id FROM leipziger_entries"))]
    for document_id in document_ids:
        links = connection.execute(
            sa.text(
                "SELECT dc.customer_id, dc.row_data, c.name FROM document_customers dc "
                "JOIN customers c ON c.id = dc.customer_id WHERE dc.document_id = :doc ORDER BY dc.id"
            ),
            {"doc": document_id},
        ).all()
        owners: dict[tuple, set] = defaultdict(set)
        index = 0
        for customer_id, row_data, customer_name in links:
            rows = json.loads(row_data) if isinstance(row_data, str) else (row_data or [])
            for row in rows:
                item = entry_values(row, customer_name, index)
                if item is None:
                    continue
                index += 1
                owners[(item["position"], item["contract_key"], item["customer_key"])].add(customer_id)
        stored = connection.execute(
            sa.text(
                "SELECT id, position, contract_key, customer_key FROM leipziger_entries "
                "WHERE document_id = :doc AND customer_id IS NULL"
            ),
            {"doc": document_id},
        ).all()
        for entry_id, position, contract_key, customer_key in stored:
            candidates = owners.get((position, contract_key, customer_key), set())
            if len(candidates) == 1:
                connection.execute(entries.update().where(entries.c.id == entry_id).values(customer_id=next(iter(candidates))))


def _backfill_customer_fields():
    """Vermittlernummer aus dem neuesten zugeordneten Vorgang, Herkunft LEIPZIGER_LISTE - nur
    fuer Kunden, bei denen diese neuen Felder noch leer sind."""
    connection = op.get_bind()
    customers = sa.table(
        "customers",
        sa.column("id", sa.Integer),
        sa.column("broker_number", sa.String),
        sa.column("source", sa.String),
    )
    rows = connection.execute(
        sa.text(
            "SELECT e.customer_id, e.broker_number FROM leipziger_entries e "
            "JOIN documents d ON d.id = e.document_id "
            "WHERE e.customer_id IS NOT NULL ORDER BY d.uploaded_at, d.id, e.position"
        )
    ).all()
    latest: dict[int, str | None] = {}
    for customer_id, broker_number in rows:
        if broker_number or customer_id not in latest:
            latest[customer_id] = broker_number
    for customer_id, broker_number in latest.items():
        connection.execute(
            customers.update()
            .where(customers.c.id == customer_id, customers.c.source.is_(None))
            .values(source="LEIPZIGER_LISTE")
        )
        if broker_number:
            connection.execute(
                customers.update()
                .where(customers.c.id == customer_id, customers.c.broker_number.is_(None))
                .values(broker_number=broker_number[:50])
            )


def downgrade():
    # Schlaegt bewusst fehl, solange Audit-Eintraege mit den neuen Werten existieren.
    _alter_audit_enum(NEW_AUDIT_VALUES, OLD_AUDIT_VALUES)
    with op.batch_alter_table("leipziger_entries", schema=None) as batch_op:
        batch_op.drop_index("ix_leipziger_entries_customer")
        batch_op.drop_constraint("fk_leipziger_entries_customer_id", type_="foreignkey")
        batch_op.drop_column("customer_id")
    with op.batch_alter_table("customers", schema=None) as batch_op:
        batch_op.drop_column("field_sources")
        batch_op.drop_column("source")
        batch_op.drop_column("broker_number")
    op.drop_table("customer_memos")
