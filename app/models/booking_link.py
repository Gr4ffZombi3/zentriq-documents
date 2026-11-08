"""Persoenliche Buchungslinks fuer Kunden ohne Anmeldung.

Der oeffentliche Link enthaelt nur ein zufaelliges Token (secrets.token_urlsafe(32)). Gespeichert
wird dessen SHA-256 (Suche) und - fuer erneutes Kopieren im Buero - eine verschluesselte Fassung.
Kunden-/Buero-IDs stehen nie im Link. Freigegeben sind ausschliesslich die gespeicherten
Startzeiten (offers); frei ist davon, was der Kalender aktuell zulaesst."""

from datetime import datetime, timezone

from app.extensions import db
from app.tenancy import TenantScopedMixin


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


class BookingLink(TenantScopedMixin, db.Model):
    __tablename__ = "booking_links"

    id = db.Column(db.Integer, primary_key=True)
    token_hash = db.Column(db.String(64), nullable=False, unique=True, index=True)
    token_enc = db.Column(db.Text, nullable=False)
    title = db.Column(db.String(120), nullable=False)  # Terminart, fuer den Kunden sichtbar
    location_kind = db.Column(db.String(20), nullable=True)
    minutes = db.Column(db.Integer, nullable=False)
    assignee_user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    customer_id = db.Column(db.Integer, db.ForeignKey("customers.id", ondelete="SET NULL"), nullable=True, index=True)
    offers = db.Column(db.JSON, nullable=False)  # ["2026-11-10T09:00", ...] Ortszeit Europe/Berlin
    expires_at = db.Column(db.DateTime, nullable=False)
    max_bookings = db.Column(db.Integer, nullable=False, default=1, server_default="1")
    booking_count = db.Column(db.Integer, nullable=False, default=0, server_default="0")
    used_at = db.Column(db.DateTime, nullable=True)
    last_submission = db.Column(db.String(64), nullable=True)  # Kennung des letzten Absendens (Doppelklick)
    revoked_at = db.Column(db.DateTime, nullable=True)
    revoked_by_user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_by_user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=_now)

    assignee = db.relationship("User", foreign_keys=[assignee_user_id])
    created_by = db.relationship("User", foreign_keys=[created_by_user_id])
    customer = db.relationship("Customer")
