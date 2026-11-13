"""Allgemeiner Buchungslink je Buero (z. B. https://zentriqai.de/termin/heller).

Der Buero-Admin legt fest: Adresse (slug), Termintyp ("Beratungstermin") mit Dauer, zustaendiger
Kalender und je Wochentag die Zeitfenster fuer die Online-Buchung. Der Link bleibt nach einer Buchung
gueltig (Signatur geeignet). Oeffentlich sichtbar sind nur freie Zeiten (app/services/office_booking.py)."""

from datetime import datetime, timezone

from app.extensions import db
from app.tenancy import TenantScopedMixin


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


class OfficeBookingPage(TenantScopedMixin, db.Model):
    __tablename__ = "office_booking_pages"
    __table_args__ = (db.UniqueConstraint("tenant_id", name="uq_office_booking_pages_tenant_id"),)

    id = db.Column(db.Integer, primary_key=True)
    slug = db.Column(db.String(40), nullable=False, unique=True)
    enabled = db.Column(db.Boolean, nullable=False, default=False, server_default=db.false())
    title = db.Column(db.String(80), nullable=False, default="Beratungstermin")
    minutes = db.Column(db.Integer, nullable=False, default=30, server_default="30")
    assignee_user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    # {"1": [["09:00", "12:00"], ["14:00", "17:00"]], ...} - ISO-Wochentag -> Zeitfenster (Ortszeit)
    windows = db.Column(db.JSON, nullable=True)
    horizon_days = db.Column(db.Integer, nullable=False, default=21, server_default="21")
    updated_at = db.Column(db.DateTime, nullable=False, default=_now, onupdate=_now)
    updated_by_user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"), nullable=True)

    assignee = db.relationship("User", foreign_keys=[assignee_user_id])
