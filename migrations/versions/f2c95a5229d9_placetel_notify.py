"""Anruferkennung ueber Placetel Notify: Zuordnung per Rufnummer/SIP-Benutzer, Anruf-Referenz

Revision ID: f2c95a5229d9
Revises: 82486878dbea
Create Date: 2026-11-12 13:00:00.000000

Rein additiv: telephony_lines.placetel_number_key / placetel_peer (neu, nullable),
telephony_lines.webex_person_uuid wird nullable (Placetel-Zuordnungen haben keine Webex-Person),
telephony_calls.external_ref (SHA-256 der Placetel-call_id, um alle Hinweise eines Anrufs zu finden).
Bestehende Daten bleiben unveraendert.
"""

import sqlalchemy as sa
from alembic import op

revision = "f2c95a5229d9"
down_revision = "82486878dbea"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("telephony_lines", schema=None) as batch_op:
        batch_op.alter_column("webex_person_uuid", existing_type=sa.String(length=64), nullable=True)
        batch_op.add_column(sa.Column("placetel_number_key", sa.String(length=20), nullable=True))
        batch_op.add_column(sa.Column("placetel_peer", sa.String(length=120), nullable=True))
        batch_op.create_index("ix_telephony_lines_placetel_number_key", ["placetel_number_key"], unique=False)
        batch_op.create_index("ix_telephony_lines_placetel_peer", ["placetel_peer"], unique=False)
    with op.batch_alter_table("telephony_calls", schema=None) as batch_op:
        batch_op.add_column(sa.Column("external_ref", sa.String(length=64), nullable=True))
        batch_op.create_index("ix_telephony_calls_external_ref", ["external_ref"], unique=False)


def downgrade():
    with op.batch_alter_table("telephony_calls", schema=None) as batch_op:
        batch_op.drop_index("ix_telephony_calls_external_ref")
        batch_op.drop_column("external_ref")
    with op.batch_alter_table("telephony_lines", schema=None) as batch_op:
        batch_op.drop_index("ix_telephony_lines_placetel_peer")
        batch_op.drop_index("ix_telephony_lines_placetel_number_key")
        batch_op.drop_column("placetel_peer")
        batch_op.drop_column("placetel_number_key")
        batch_op.alter_column("webex_person_uuid", existing_type=sa.String(length=64), nullable=False)
