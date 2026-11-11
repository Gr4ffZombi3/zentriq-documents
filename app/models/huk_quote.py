"""Kfz-Beitragsberechnung ueber den oeffentlichen HUK-Webrechner (Hintergrundauftrag).

Status: "draft" (Angaben werden gesammelt) -> "queued" (bestaetigt, wartet) -> "running"
-> "done" | "needs_input" (Rechner stellt eine nicht beantwortbare Frage) | "failed" |
"cancelled". Ergebnisse stammen ausschliesslich aus einer erfolgreich durchgefuehrten Berechnung
im Rechner (result.source = "huk.de"); es gibt keine geschaetzten Beitraege."""

from datetime import datetime, timezone

from app.extensions import db
from app.tenancy import TenantScopedMixin


def utcnow_naive():
    return datetime.now(timezone.utc).replace(tzinfo=None)


ACTIVE_STATUSES = ("queued", "running")


class HukQuoteJob(TenantScopedMixin, db.Model):
    __tablename__ = "huk_quote_jobs"
    __table_args__ = (
        db.UniqueConstraint("tenant_id", "created_by_user_id", "client_ref", name="uq_huk_quote_jobs_client_ref"),
        db.Index("ix_huk_quote_jobs_tenant_status", "tenant_id", "status"),
    )

    id = db.Column(db.Integer, primary_key=True)
    status = db.Column(db.String(20), nullable=False, default="draft", server_default="draft")
    customer_id = db.Column(db.Integer, db.ForeignKey("customers.id", ondelete="SET NULL"), nullable=True, index=True)
    # Bestaetigte Angaben (siehe app/services/huk/schema.py). Personenbezogen - nur im Mandanten.
    input_data = db.Column(db.JSON, nullable=True)
    # Vom Benutzer bestaetigt (Startaktion "Angebot auf huk.de berechnen").
    confirmed_at = db.Column(db.DateTime, nullable=True)
    confirmed_by_user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    # Ergebnis nur nach erfolgreicher Rechner-Berechnung.
    result = db.Column(db.JSON, nullable=True)
    # Verstaendliche Ursache bzw. offene Frage fuer den Benutzer (ohne technische Details).
    message = db.Column(db.String(500), nullable=True)
    error_code = db.Column(db.String(40), nullable=True)
    celery_task_id = db.Column(db.String(64), nullable=True)
    attempts = db.Column(db.Integer, nullable=False, default=0, server_default="0")
    cancel_requested = db.Column(db.Boolean, nullable=False, default=False, server_default=db.false())
    ai_calls = db.Column(db.Integer, nullable=False, default=0, server_default="0")
    ai_cost_usd = db.Column(db.Numeric(10, 4), nullable=False, default=0, server_default="0")
    # Kfz-Vergleich aus Versicherungsschein (app/services/huk/policy.py): bisheriger Vertrag laut
    # Dokument, Pruefhinweise, ggf. manuell eingetragener HUK-Beitrag. Geht nie an huk.de.
    comparison = db.Column(db.JSON, nullable=True)
    # Beleg der Rechnerseite (Screenshot des Angebotsbereichs, ohne Namen/Adressen).
    evidence_path = db.Column(db.String(255), nullable=True)
    client_ref = db.Column(db.String(64), nullable=True)
    created_by_user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow_naive)
    started_at = db.Column(db.DateTime, nullable=True)
    finished_at = db.Column(db.DateTime, nullable=True)
    updated_at = db.Column(db.DateTime, nullable=False, default=utcnow_naive, onupdate=utcnow_naive)

    customer = db.relationship("Customer", foreign_keys=[customer_id])
    created_by = db.relationship("User", foreign_keys=[created_by_user_id])

    @property
    def is_active(self) -> bool:
        return self.status in ACTIVE_STATUSES
