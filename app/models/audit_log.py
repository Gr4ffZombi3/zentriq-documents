import enum
from datetime import datetime, timezone

from app.extensions import db


class AuditEventType(enum.Enum):
    LOGIN_SUCCESS = "login_success"
    LOGIN_FAILED = "login_failed"
    LOGOUT = "logout"


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
