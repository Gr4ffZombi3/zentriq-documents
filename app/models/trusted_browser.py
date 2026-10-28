from datetime import datetime, timezone

from app.extensions import db


def _utcnow_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class TrustedBrowser(db.Model):
    """Browser, in dem ein Benutzer nach erfolgreicher 2FA "Diesem Browser 30 Tage vertrauen"
    gewaehlt hat (app/services/trusted_browsers.py).

    Im Cookie liegt nur ein zufaelliges Token, gespeichert wird ausschliesslich dessen SHA-256-
    Hash. Gueltig nur bis expires_at, solange nicht widerrufen und solange auth_version noch der
    des Benutzers entspricht (Passwortaenderung, 2FA-Reset, Deaktivierung ... erhoehen sie).
    Absichtlich NICHT TenantScopedMixin: wird beim Login vor dem Setzen des Tenant-Kontexts
    ausschliesslich ueber user_id + Token-Hash abgefragt."""

    __tablename__ = "trusted_browsers"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    token_hash = db.Column(db.String(64), nullable=False, unique=True)
    auth_version = db.Column(db.Integer, nullable=False, default=0)
    created_at = db.Column(db.DateTime, nullable=False, default=_utcnow_naive)
    expires_at = db.Column(db.DateTime, nullable=False)
    last_used_at = db.Column(db.DateTime, nullable=True)
    revoked_at = db.Column(db.DateTime, nullable=True)
    ip_address = db.Column(db.String(64), nullable=True)
    user_agent = db.Column(db.String(255), nullable=True)

    def __repr__(self):
        return f"<TrustedBrowser {self.id} user={self.user_id}>"
