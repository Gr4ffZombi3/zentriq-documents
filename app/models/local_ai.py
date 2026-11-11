"""Hybridbetrieb KI: eigener Rechner (Windows-Laptop) mit lokalem Modell.

- LocalWorker: widerrufbarer Zugang eines Rechners fuer genau ein Buero. Gespeichert wird nur der
  SHA-256-Hash des Zugangsschluessels (der Schluessel selbst wird einmal angezeigt).
- LocalAiJob: Auftrag fuer den Rechner. Der Rechner holt Auftraege per HTTPS ab (Verbindung geht
  vom Laptop aus), bearbeitet sie mit dem lokalen Modell und meldet das Ergebnis zurueck.
  Status: queued -> running -> done | failed | cancelled. Ein abgebrochener Lauf (Lease abgelaufen)
  wird erneut vergeben; Ergebnisse werden nur einmal uebernommen (lease_id + Statuswechsel).
  Die Eingabedaten (payload, ggf. Bilder) werden nach Abschluss geloescht."""

from datetime import datetime, timezone

from app.extensions import db
from app.tenancy import TenantScopedMixin


def utcnow_naive():
    return datetime.now(timezone.utc).replace(tzinfo=None)


class LocalWorker(TenantScopedMixin, db.Model):
    __tablename__ = "local_workers"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(80), nullable=False)
    token_hash = db.Column(db.String(64), nullable=False, unique=True)
    # Erste Zeichen des Schluessels zur Wiedererkennung in der Oberflaeche (kein Geheimnis).
    token_prefix = db.Column(db.String(16), nullable=False)
    created_by_user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow_naive)
    revoked_at = db.Column(db.DateTime, nullable=True)
    last_seen_at = db.Column(db.DateTime, nullable=True)
    # Vom Rechner gemeldet (nur zur Anzeige): lokales Modell und Worker-Version.
    model_name = db.Column(db.String(120), nullable=True)
    worker_version = db.Column(db.String(30), nullable=True)

    @property
    def is_active(self) -> bool:
        return self.revoked_at is None


class LocalAiJob(TenantScopedMixin, db.Model):
    __tablename__ = "local_ai_jobs"
    __table_args__ = (db.Index("ix_local_ai_jobs_tenant_status", "tenant_id", "status"),)

    id = db.Column(db.Integer, primary_key=True)
    kind = db.Column(db.String(40), nullable=False)
    status = db.Column(db.String(20), nullable=False, default="queued", server_default="queued")
    created_by_user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    # Wozu der Auftrag gehoert, z. B. ("huk_quote_job", 12) oder ("ai_message", 345).
    origin_type = db.Column(db.String(30), nullable=True)
    origin_id = db.Column(db.Integer, nullable=True, index=True)
    # Eingabe fuer den Rechner (Prompt, ggf. Bilder als Base64). Wird nach Abschluss geleert.
    payload = db.Column(db.JSON, nullable=True)
    # Ergebnis des lokalen Modells (Text) - wird von der Zentriq-Funktion ausgewertet.
    result = db.Column(db.JSON, nullable=True)
    message = db.Column(db.String(300), nullable=True)
    worker_id = db.Column(db.Integer, db.ForeignKey("local_workers.id", ondelete="SET NULL"), nullable=True)
    lease_id = db.Column(db.String(64), nullable=True)
    lease_until = db.Column(db.DateTime, nullable=True)
    attempts = db.Column(db.Integer, nullable=False, default=0, server_default="0")
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow_naive)
    started_at = db.Column(db.DateTime, nullable=True)
    finished_at = db.Column(db.DateTime, nullable=True)
    updated_at = db.Column(db.DateTime, nullable=False, default=utcnow_naive, onupdate=utcnow_naive)

    worker = db.relationship("LocalWorker")

    @property
    def is_open(self) -> bool:
        return self.status in ("queued", "running")
