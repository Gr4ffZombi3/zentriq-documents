from datetime import datetime, timezone

from app.extensions import db


def _utcnow_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class UserSession(db.Model):
    """Eine angemeldete Sitzung (Browser/Geraet) fuer "Mein Konto -> Aktive Sitzungen".

    Im Session-Cookie liegt nur ein zufaelliges Token, gespeichert wird ausschliesslich dessen
    SHA-256-Hash. Absichtlich NICHT TenantScopedMixin: wird im user_loader (vor dem Setzen des
    Tenant-Kontexts) ausschliesslich ueber user_id + Token-Hash abgefragt."""

    __tablename__ = "user_sessions"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    token_hash = db.Column(db.String(64), nullable=False, unique=True)
    created_at = db.Column(db.DateTime, nullable=False, default=_utcnow_naive)
    last_seen_at = db.Column(db.DateTime, nullable=False, default=_utcnow_naive)
    ip_address = db.Column(db.String(64), nullable=True)
    user_agent = db.Column(db.String(255), nullable=True)
    revoked_at = db.Column(db.DateTime, nullable=True)

    @property
    def is_revoked(self) -> bool:
        return self.revoked_at is not None
