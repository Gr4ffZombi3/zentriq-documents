"""Rollenmodell SUPER_ADMIN/OFFICE_ADMIN/EMPLOYEE, Zwei-Faktor-Authentifizierung, Soft-Delete

Revision ID: e5f6a7b8c9d0
Revises: c3d4e5f6a7b8
Create Date: 2026-10-01 14:00:00.000000

Bestehende Konten behalten ihre Rechte: ADMIN -> OFFICE_ADMIN, MITARBEITER -> EMPLOYEE.
Passwoerter und Logins bleiben unveraendert. Ein SUPER_ADMIN wird bewusst NICHT hier, sondern
per CLI (`flask grant-super-admin`) vergeben.
"""

import sqlalchemy as sa
from alembic import op

revision = "e5f6a7b8c9d0"
down_revision = "c3d4e5f6a7b8"
branch_labels = None
depends_on = None

OLD_ROLES = ("ADMIN", "MITARBEITER")
NEW_ROLES = ("SUPER_ADMIN", "OFFICE_ADMIN", "EMPLOYEE")

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
)
NEW_AUDIT_VALUES = OLD_AUDIT_VALUES + (
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
)


def _alter_role_enum(from_values, to_values, server_default):
    with op.batch_alter_table("users", schema=None) as batch_op:
        batch_op.alter_column(
            "role",
            existing_type=sa.Enum(*from_values, name="userrole"),
            type_=sa.Enum(*to_values, name="userrole"),
            existing_nullable=False,
            server_default=server_default,
        )


def upgrade():
    # 1. Rollen: Enum erweitern, Werte umschreiben, alte Werte entfernen.
    _alter_role_enum(OLD_ROLES, OLD_ROLES + NEW_ROLES, "MITARBEITER")
    op.execute("UPDATE users SET role = 'OFFICE_ADMIN' WHERE role = 'ADMIN'")
    op.execute("UPDATE users SET role = 'EMPLOYEE' WHERE role = 'MITARBEITER'")
    _alter_role_enum(OLD_ROLES + NEW_ROLES, NEW_ROLES, "EMPLOYEE")

    # 2. Soft-Delete, Session-Invalidierung, TOTP.
    with op.batch_alter_table("users", schema=None) as batch_op:
        batch_op.add_column(sa.Column("deleted_at", sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column("deleted_by_user_id", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("auth_version", sa.Integer(), nullable=False, server_default="0"))
        batch_op.add_column(sa.Column("totp_secret_encrypted", sa.String(length=255), nullable=True))
        batch_op.add_column(sa.Column("totp_enabled_at", sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column("totp_last_counter", sa.BigInteger(), nullable=True))
        batch_op.create_foreign_key(
            "fk_users_deleted_by_user_id", "users", ["deleted_by_user_id"], ["id"], ondelete="SET NULL"
        )

    # 3. Recovery Codes (nur HMAC gespeichert).
    op.create_table(
        "user_recovery_codes",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("code_hash", sa.String(length=128), nullable=False),
        sa.Column("used_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("user_recovery_codes", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_user_recovery_codes_user_id"), ["user_id"], unique=False)

    # 4. Neue Audit-Ereignisse.
    with op.batch_alter_table("audit_logs", schema=None) as batch_op:
        batch_op.alter_column(
            "event_type",
            existing_type=sa.Enum(*OLD_AUDIT_VALUES, name="auditeventtype"),
            type_=sa.Enum(*NEW_AUDIT_VALUES, name="auditeventtype"),
            existing_nullable=False,
        )


def downgrade():
    # Schlaegt bewusst fehl, solange Audit-Eintraege mit den neuen Event-Typen existieren
    # (Audit-Eintraege werden nicht geloescht).
    with op.batch_alter_table("audit_logs", schema=None) as batch_op:
        batch_op.alter_column(
            "event_type",
            existing_type=sa.Enum(*NEW_AUDIT_VALUES, name="auditeventtype"),
            type_=sa.Enum(*OLD_AUDIT_VALUES, name="auditeventtype"),
            existing_nullable=False,
        )
    op.drop_table("user_recovery_codes")
    with op.batch_alter_table("users", schema=None) as batch_op:
        batch_op.drop_constraint("fk_users_deleted_by_user_id", type_="foreignkey")
        batch_op.drop_column("totp_last_counter")
        batch_op.drop_column("totp_enabled_at")
        batch_op.drop_column("totp_secret_encrypted")
        batch_op.drop_column("auth_version")
        batch_op.drop_column("deleted_by_user_id")
        batch_op.drop_column("deleted_at")

    # SUPER_ADMIN gibt es im alten Modell nicht - faellt auf ADMIN zurueck.
    _alter_role_enum(NEW_ROLES, OLD_ROLES + NEW_ROLES, "EMPLOYEE")
    op.execute("UPDATE users SET role = 'ADMIN' WHERE role IN ('OFFICE_ADMIN', 'SUPER_ADMIN')")
    op.execute("UPDATE users SET role = 'MITARBEITER' WHERE role = 'EMPLOYEE'")
    _alter_role_enum(OLD_ROLES + NEW_ROLES, OLD_ROLES, "MITARBEITER")
