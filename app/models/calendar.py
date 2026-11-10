"""Gemeinsamer Buerokalender.

Zeiten werden als naives UTC gespeichert (wie die Zeiterfassung); Anzeige und Eingabe in
APP_TIMEZONE (Europe/Berlin, inkl. Sommer-/Winterzeit). Jede Aenderung landet zusaetzlich in
CalendarEventChange (alt/neu, Akteur, Zeitpunkt) - auch nach dem Loeschen eines Termins bleibt
der Verlauf erhalten. `version` dient der Erkennung gleichzeitiger Aenderungen (optimistische
Sperre), `client_ref` verhindert doppeltes Speichern bei Doppelklick/erneutem Absenden."""

from datetime import datetime, timezone

from app.extensions import db
from app.tenancy import TenantScopedMixin


def utcnow_naive():
    return datetime.now(timezone.utc).replace(tzinfo=None)


class CalendarEvent(TenantScopedMixin, db.Model):
    __tablename__ = "calendar_events"
    __table_args__ = (
        db.Index("ix_calendar_events_tenant_range", "tenant_id", "starts_at", "ends_at"),
        db.Index("ix_calendar_events_assignee_range", "assignee_user_id", "starts_at"),
        db.UniqueConstraint("tenant_id", "created_by_user_id", "client_ref", name="uq_calendar_events_client_ref"),
    )

    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(200), nullable=False)
    starts_at = db.Column(db.DateTime, nullable=False)
    ends_at = db.Column(db.DateTime, nullable=False)
    assignee_user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    customer_id = db.Column(db.Integer, db.ForeignKey("customers.id", ondelete="SET NULL"), nullable=True, index=True)
    # Termin zu einem Vorgang ohne Kundenakte (app/models/pending_case.py); bleibt nach dem
    # Verknuepfen als Herkunft stehen, customer_id wird dann gesetzt.
    pending_case_id = db.Column(db.Integer, db.ForeignKey("pending_cases.id", ondelete="SET NULL"), nullable=True, index=True)
    # "vor_ort" | "telefon" | "video" | None
    location_kind = db.Column(db.String(20), nullable=True)
    location = db.Column(db.String(255), nullable=True)
    note = db.Column(db.Text, nullable=True)
    # "scheduled" | "cancelled"
    status = db.Column(db.String(20), nullable=False, default="scheduled", server_default="scheduled")
    version = db.Column(db.Integer, nullable=False, default=1, server_default="1")
    client_ref = db.Column(db.String(64), nullable=True)
    created_by_user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow_naive)
    updated_by_user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    updated_at = db.Column(db.DateTime, nullable=False, default=utcnow_naive, index=True)

    assignee = db.relationship("User", foreign_keys=[assignee_user_id])
    customer = db.relationship("Customer", foreign_keys=[customer_id])
    pending_case = db.relationship("PendingCase", foreign_keys=[pending_case_id])
    created_by = db.relationship("User", foreign_keys=[created_by_user_id])
    updated_by = db.relationship("User", foreign_keys=[updated_by_user_id])

    @property
    def is_cancelled(self) -> bool:
        return self.status == "cancelled"


class CalendarEventChange(TenantScopedMixin, db.Model):
    """Aenderungsverlauf. Bewusst ohne Fremdschluessel auf den Termin, damit der Verlauf eines
    geloeschten Termins erhalten bleibt."""

    __tablename__ = "calendar_event_changes"

    id = db.Column(db.Integer, primary_key=True)
    event_id = db.Column(db.Integer, nullable=False, index=True)
    # "create" | "update" | "cancel" | "reactivate" | "delete"
    action = db.Column(db.String(20), nullable=False)
    changes = db.Column(db.JSON, nullable=True)
    actor_user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    actor_email_snapshot = db.Column(db.String(255), nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow_naive)
