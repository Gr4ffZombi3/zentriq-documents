"""Kunden: Vergleichsschluessel fuer Dubletten-Erkennung und Memo-Kundenerkennung

Revision ID: c4d5e6f7a8b9
Revises: b3c4d5e6f7a8
Create Date: 2026-10-02 12:00:00.000000

Neue nullable Spalten customers.name_key/phone_key/customer_number_key mit Indizes (je Mandant)
und einmaliges Befuellen aus den vorhandenen Werten (nur diese drei neuen Spalten werden
geschrieben). Neue Enum-Werte CUSTOMER_MERGED fuer Audit-Log und Kundenverlauf.
"""

import sqlalchemy as sa
from alembic import op

revision = "c4d5e6f7a8b9"
down_revision = "b3c4d5e6f7a8"
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
)
NEW_AUDIT_VALUES = OLD_AUDIT_VALUES + ("CUSTOMER_MERGED",)

OLD_TIMELINE_VALUES = (
    "DOCUMENT_UPLOADED",
    "OFFER_DETECTED",
    "NEW_CONTRACT_DETECTED",
    "VEHICLE_CHANGE_DETECTED",
    "STORNO_DETECTED",
    "TASK_CREATED",
    "TASK_STATUS_CHANGED",
    "LIST_COMPARISON_CHANGE",
)
NEW_TIMELINE_VALUES = OLD_TIMELINE_VALUES + ("CUSTOMER_MERGED",)

BATCH_SIZE = 1000


def _alter_enum(table, name, from_values, to_values):
    with op.batch_alter_table(table, schema=None) as batch_op:
        batch_op.alter_column(
            "event_type",
            existing_type=sa.Enum(*from_values, name=name),
            type_=sa.Enum(*to_values, name=name),
            existing_nullable=False,
        )


def upgrade():
    with op.batch_alter_table("customers", schema=None) as batch_op:
        batch_op.add_column(sa.Column("name_key", sa.String(length=255), nullable=True))
        batch_op.add_column(sa.Column("phone_key", sa.String(length=20), nullable=True))
        batch_op.add_column(sa.Column("customer_number_key", sa.String(length=50), nullable=True))
        batch_op.create_index("ix_customers_tenant_name_key", ["tenant_id", "name_key"], unique=False)
        batch_op.create_index("ix_customers_tenant_phone_key", ["tenant_id", "phone_key"], unique=False)
        batch_op.create_index("ix_customers_tenant_customer_number_key", ["tenant_id", "customer_number_key"], unique=False)

    _backfill()
    _alter_enum("audit_logs", "auditeventtype", OLD_AUDIT_VALUES, NEW_AUDIT_VALUES)
    _alter_enum("customer_timeline_events", "timelineeventtype", OLD_TIMELINE_VALUES, NEW_TIMELINE_VALUES)


def _backfill():
    # Dieselbe Berechnung wie im Modell (reine Funktion ohne Modellzugriff).
    from app.utils.customer_keys import customer_keys

    connection = op.get_bind()
    customers = sa.table(
        "customers",
        sa.column("id", sa.Integer),
        sa.column("name_key", sa.String),
        sa.column("phone_key", sa.String),
        sa.column("customer_number_key", sa.String),
    )
    last_id = 0
    while True:
        rows = connection.execute(
            sa.text(
                "SELECT id, name, phone, customer_number FROM customers WHERE id > :last ORDER BY id LIMIT :size"
            ),
            {"last": last_id, "size": BATCH_SIZE},
        ).all()
        if not rows:
            break
        for customer_id, name, phone, customer_number in rows:
            connection.execute(
                customers.update().where(customers.c.id == customer_id).values(**customer_keys(name, phone, customer_number))
            )
        last_id = rows[-1][0]


def downgrade():
    # Schlaegt bewusst fehl, solange Eintraege mit CUSTOMER_MERGED existieren.
    _alter_enum("customer_timeline_events", "timelineeventtype", NEW_TIMELINE_VALUES, OLD_TIMELINE_VALUES)
    _alter_enum("audit_logs", "auditeventtype", NEW_AUDIT_VALUES, OLD_AUDIT_VALUES)
    with op.batch_alter_table("customers", schema=None) as batch_op:
        batch_op.drop_index("ix_customers_tenant_customer_number_key")
        batch_op.drop_index("ix_customers_tenant_phone_key")
        batch_op.drop_index("ix_customers_tenant_name_key")
        batch_op.drop_column("customer_number_key")
        batch_op.drop_column("phone_key")
        batch_op.drop_column("name_key")
