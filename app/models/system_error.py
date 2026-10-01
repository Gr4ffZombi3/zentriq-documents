from datetime import datetime, timezone

from app.extensions import db


class SystemErrorEvent(db.Model):
    """Technisches Fehlerprotokoll fuer das Plattform-Panel (SUPER_ADMIN).

    Absichtlich ohne Inhalte: keine Fehlermeldung, kein Stacktrace, keine Query-Parameter - nur
    Zeitpunkt, Herkunft, Ort und Fehlerklasse. Damit gelangen keine Kunden-, Memo- oder
    Leipziger-Daten eines Bueros in die Plattformverwaltung. Nicht mandantengebunden; die
    Buero-ID dient nur der Zuordnung. Details stehen weiterhin im Server-Log."""

    __tablename__ = "system_error_events"

    id = db.Column(db.Integer, primary_key=True)
    occurred_at = db.Column(db.DateTime, nullable=False, default=lambda: datetime.now(timezone.utc), index=True)
    # "request" (unbehandelte Ausnahme in einer Anfrage), "task" (Hintergrundaufgabe),
    # "import" (Auswertung einer hochgeladenen Liste fehlgeschlagen).
    source = db.Column(db.String(20), nullable=False)
    location = db.Column(db.String(255), nullable=True)
    error_type = db.Column(db.String(120), nullable=False)
    tenant_id = db.Column(db.Integer, nullable=True)

    def __repr__(self):
        return f"<SystemErrorEvent {self.id} {self.source} {self.error_type}>"
