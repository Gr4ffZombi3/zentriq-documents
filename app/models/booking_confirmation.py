"""Bestaetigungs-E-Mail fuer Termine, die Kunden ueber einen oeffentlichen Buchungslink gebucht haben.

Genau eine Zeile je Kalendertermin (event_id eindeutig) - Doppelklick, Neuladen oder doppelt
eingeplante Aufgaben erzeugen keine zweite Mail. Der Versand beansprucht die Zeile vorher atomar
(pending -> sending); nur wer das schafft, sendet.

status: pending (eingeplant), sending (wird gesendet), sent, failed (Fehlerklasse in error).
Gespeichert wird nur, was fuer Versand und Fehleranzeige im Buero noetig ist - der Mailtext wird beim
Versand aus Termin und Bueroprofil erzeugt."""

from app.extensions import db
from app.services.timetracking.clock import utcnow_naive
from app.tenancy import TenantScopedMixin


class BookingConfirmation(TenantScopedMixin, db.Model):
    __tablename__ = "booking_confirmations"
    __table_args__ = (db.UniqueConstraint("event_id", name="uq_booking_confirmations_event"),)

    id = db.Column(db.Integer, primary_key=True)
    event_id = db.Column(db.Integer, db.ForeignKey("calendar_events.id", ondelete="CASCADE"), nullable=False)
    recipient = db.Column(db.String(255), nullable=False)
    name = db.Column(db.String(120), nullable=True)  # Anrede in der Mail
    phone = db.Column(db.String(30), nullable=True)  # nur fuer den Hinweis beim Telefontermin
    title = db.Column(db.String(120), nullable=False)  # Terminart wie auf der Buchungsseite
    status = db.Column(db.String(10), nullable=False, default="pending", server_default="pending", index=True)
    attempts = db.Column(db.Integer, nullable=False, default=0, server_default="0")
    error = db.Column(db.String(120), nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow_naive)
    queued_at = db.Column(db.DateTime, nullable=False, default=utcnow_naive)  # letzte Einplanung (auch "Erneut senden")
    sent_at = db.Column(db.DateTime, nullable=True)
