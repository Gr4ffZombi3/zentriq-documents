"""Anruferkennung ueber Webex Calling (erster Test: eine Durchwahl je Zentriq-Benutzer).

- TelephonyLine: ordnet einen Webex-Calling-Benutzer (Person-ID aus dem Webhook-Feld actorId) genau
  einem Zentriq-Benutzer zu. Das Buero ergibt sich aus dem Zentriq-Benutzer.
- TelephonyCall: ein eingehender Anruf auf einer zugeordneten Durchwahl. call_key (SHA-256 der
  Webex-callId) ist eindeutig - doppelt zugestellte Meldungen loesen den Hinweis nur einmal aus.

Die Anrufernummer wird nur hier gespeichert (fuer den Hinweis und die Rueckrufnotiz), nie in Logs."""

from datetime import datetime, timezone

from app.extensions import db
from app.tenancy import TenantScopedMixin


def _now():
    return datetime.now(timezone.utc)


class TelephonyLine(TenantScopedMixin, db.Model):
    __tablename__ = "telephony_lines"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, unique=True)
    # UUID der Webex-Person (aus der Base64-Person-ID dekodiert, Kleinbuchstaben).
    webex_person_uuid = db.Column(db.String(64), nullable=False, unique=True)
    # Anzeige fuer die Verwaltung, z. B. "Durchwahl 21".
    label = db.Column(db.String(60), nullable=True)
    active = db.Column(db.Boolean, nullable=False, default=True, server_default=db.true())
    created_at = db.Column(db.DateTime, nullable=False, default=_now)

    user = db.relationship("User")

    def __repr__(self):
        return f"<TelephonyLine {self.id} user={self.user_id}>"


class TelephonyCall(TenantScopedMixin, db.Model):
    __tablename__ = "telephony_calls"
    __table_args__ = (db.Index("ix_telephony_calls_user_created", "user_id", "created_at"),)

    id = db.Column(db.Integer, primary_key=True)
    call_key = db.Column(db.String(64), nullable=False, unique=True)
    line_id = db.Column(db.Integer, db.ForeignKey("telephony_lines.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    # Rufnummer wie von Webex geliefert (gekuerzt); leer bei unterdrueckter Nummer.
    remote_number = db.Column(db.String(50), nullable=True)
    privacy = db.Column(db.Boolean, nullable=False, default=False)
    # Webex callType: external, location, organization, ...
    call_type = db.Column(db.String(20), nullable=True)
    # "ringing" | "answered" | "ended"
    status = db.Column(db.String(20), nullable=False, default="ringing")
    created_at = db.Column(db.DateTime, nullable=False, default=_now)
    ended_at = db.Column(db.DateTime, nullable=True)

    line = db.relationship("TelephonyLine")

    def __repr__(self):
        return f"<TelephonyCall {self.id} {self.status}>"
