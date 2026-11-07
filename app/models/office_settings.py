"""Buero-Einstellungen: Buero-Profil (Absender/Signatur fuer E-Mail-Entwuerfe) und Regeln fuer
die Terminplanung (Bueroezeiten, Termindauer, Puffer, Vorlauf). Eine Zeile je Buero; fehlt sie
oder sind keine Buerozeiten hinterlegt, schlaegt der Assistent keine Termine vor, sondern bittet
um die einmalige Einrichtung (Kalender > Einstellungen)."""

from datetime import datetime, timezone

from app.extensions import db
from app.tenancy import TenantScopedMixin


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


class OfficeSettings(TenantScopedMixin, db.Model):
    __tablename__ = "office_settings"
    __table_args__ = (db.UniqueConstraint("tenant_id", name="uq_office_settings_tenant"),)

    id = db.Column(db.Integer, primary_key=True)
    # Buero-Profil
    office_name = db.Column(db.String(200), nullable=True)
    email = db.Column(db.String(255), nullable=True)
    phone = db.Column(db.String(50), nullable=True)
    address = db.Column(db.String(255), nullable=True)
    website = db.Column(db.String(255), nullable=True)
    signature = db.Column(db.Text, nullable=True)
    # Terminplanung: {"1": ["09:00", "17:00"], ..., "7": null} (ISO-Wochentag, Ortszeit Europe/Berlin)
    hours = db.Column(db.JSON, nullable=True)
    default_minutes = db.Column(db.Integer, nullable=False, default=30, server_default="30")
    buffer_minutes = db.Column(db.Integer, nullable=False, default=0, server_default="0")
    lead_minutes = db.Column(db.Integer, nullable=False, default=120, server_default="120")
    horizon_days = db.Column(db.Integer, nullable=False, default=14, server_default="14")
    updated_at = db.Column(db.DateTime, nullable=False, default=_now, onupdate=_now)
    updated_by_user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
