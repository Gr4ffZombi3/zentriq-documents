import enum
from datetime import datetime, timezone

from app.extensions import db


class AuditEventType(enum.Enum):
    LOGIN_SUCCESS = "login_success"
    LOGIN_FAILED = "login_failed"
    LOGOUT = "logout"
    PASSWORD_RESET_REQUESTED = "password_reset_requested"
    PASSWORD_RESET_COMPLETED = "password_reset_completed"
    USER_CREATED = "user_created"
    USER_UPDATED = "user_updated"
    TIME_CORRECTED = "time_corrected"
    TIME_CORRECTION_REQUESTED = "time_correction_requested"
    TIME_CORRECTION_DECIDED = "time_correction_decided"
    USER_DELETED = "user_deleted"
    PASSWORD_CHANGED = "password_changed"
    PASSWORD_RESET_TRIGGERED = "password_reset_triggered"
    TWO_FACTOR_ENABLED = "two_factor_enabled"
    TWO_FACTOR_RESET = "two_factor_reset"
    TWO_FACTOR_FAILED = "two_factor_failed"
    RECOVERY_CODE_USED = "recovery_code_used"
    RECOVERY_CODES_REGENERATED = "recovery_codes_regenerated"
    TENANT_CREATED = "tenant_created"
    TENANT_UPDATED = "tenant_updated"
    # Aktivitaetsprotokoll des Bueros (ohne fachliche Inhalte: keine Memo-Texte, keine
    # Kundendaten - nur wer wann was ausgeloest hat).
    TIME_CLOCK_IN = "time_clock_in"
    TIME_CLOCK_OUT = "time_clock_out"
    MEMO_TRANSCRIBED = "memo_transcribed"
    LEIPZIGER_LIST_UPLOADED = "leipziger_list_uploaded"
    SESSIONS_REVOKED = "sessions_revoked"
    CUSTOMER_MERGED = "customer_merged"


# Sicherheitsrelevante Ereignisse ohne fachlichen Inhalt - nur diese sind fuer den
# SUPER_ADMIN sichtbar (keine Zeiterfassungs-Events).
SECURITY_EVENT_TYPES = (
    AuditEventType.LOGIN_SUCCESS,
    AuditEventType.LOGIN_FAILED,
    AuditEventType.PASSWORD_RESET_REQUESTED,
    AuditEventType.PASSWORD_RESET_COMPLETED,
    AuditEventType.PASSWORD_RESET_TRIGGERED,
    AuditEventType.PASSWORD_CHANGED,
    AuditEventType.USER_CREATED,
    AuditEventType.USER_UPDATED,
    AuditEventType.USER_DELETED,
    AuditEventType.TWO_FACTOR_ENABLED,
    AuditEventType.TWO_FACTOR_RESET,
    AuditEventType.TWO_FACTOR_FAILED,
    AuditEventType.RECOVERY_CODE_USED,
    AuditEventType.RECOVERY_CODES_REGENERATED,
    AuditEventType.TENANT_CREATED,
    AuditEventType.TENANT_UPDATED,
    AuditEventType.SESSIONS_REVOKED,
)

# Ereignisse, die der Buero-Admin unter "Aktivitaeten" sieht (nur eigener Mandant).
OFFICE_ACTIVITY_EVENT_TYPES = (
    AuditEventType.USER_CREATED,
    AuditEventType.USER_UPDATED,
    AuditEventType.USER_DELETED,
    AuditEventType.PASSWORD_CHANGED,
    AuditEventType.PASSWORD_RESET_TRIGGERED,
    AuditEventType.TWO_FACTOR_ENABLED,
    AuditEventType.TWO_FACTOR_RESET,
    AuditEventType.TIME_CLOCK_IN,
    AuditEventType.TIME_CLOCK_OUT,
    AuditEventType.TIME_CORRECTED,
    AuditEventType.TIME_CORRECTION_REQUESTED,
    AuditEventType.TIME_CORRECTION_DECIDED,
    AuditEventType.MEMO_TRANSCRIBED,
    AuditEventType.LEIPZIGER_LIST_UPLOADED,
    AuditEventType.SESSIONS_REVOKED,
    AuditEventType.CUSTOMER_MERGED,
)


class AuditLog(db.Model):
    """Absichtlich NICHT TenantScopedMixin: ein fehlgeschlagener Login mit unbekannter
    E-Mail hat keinen bestimmbaren Tenant (Login erfolgt global per E-Mail, ohne
    Tenant-Auswahl). tenant_id ist daher nullable. Kein Update-/Delete-Pfad im Code -
    Application-Level Append-Only (echte DB-Immutability ist dokumentierte Nacharbeit,
    siehe docs/SECURITY.md ab M14)."""

    __tablename__ = "audit_logs"

    id = db.Column(db.Integer, primary_key=True)
    tenant_id = db.Column(
        db.Integer, db.ForeignKey("tenants.id", ondelete="SET NULL"), nullable=True, index=True
    )
    actor_user_id = db.Column(
        db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    actor_email_snapshot = db.Column(db.String(255), nullable=True)
    event_type = db.Column(db.Enum(AuditEventType), nullable=False, index=True)
    details = db.Column(db.JSON, nullable=True)
    ip_address = db.Column(db.String(64), nullable=True)
    user_agent = db.Column(db.String(255), nullable=True)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc), index=True)

    def __repr__(self):
        return f"<AuditLog {self.id} {self.event_type}>"
