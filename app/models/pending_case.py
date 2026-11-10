"""Vorgang ohne Kundenakte: Notizen und Termine zu einer Person, die (noch) keine Zentriq-Kundenakte
hat. Auffindbar ueber Name oder Versicherungsnehmernummer; gekennzeichnet als "Noch keiner
Kundenakte zugeordnet", bis der Vorgang verknuepft ist (automatisch beim Import der Leipziger
Liste oder per Auswahl, app/services/pending_cases.py).

Beim Verknuepfen werden die Notizen als Kundennotizen mit Originaltext, Verfasser und Zeitpunkt in
die Akte uebernommen (customer_note_id merkt die Uebernahme - kein zweites Mal), Termine erhalten
die Kundenakte. Der Vorgang selbst bleibt als Nachweis erhalten."""

from datetime import datetime, timezone

from app.extensions import db
from app.tenancy import TenantScopedMixin


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


class PendingCase(TenantScopedMixin, db.Model):
    __tablename__ = "pending_cases"
    __table_args__ = (
        db.Index("ix_pending_cases_tenant_status", "tenant_id", "status"),
        db.Index("ix_pending_cases_tenant_vn_key", "tenant_id", "vn_key"),
        db.Index("ix_pending_cases_tenant_name_key", "tenant_id", "name_key"),
    )

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(200), nullable=False)
    name_key = db.Column(db.String(255), nullable=True)
    # Anzeige "808/123456-E" und Vergleichsschluessel "808123456E" (app/utils/customer_keys.vn_key)
    vn_number = db.Column(db.String(20), nullable=True)
    vn_key = db.Column(db.String(12), nullable=True)
    # "open" (noch keiner Akte zugeordnet) | "linked"
    status = db.Column(db.String(20), nullable=False, default="open", server_default="open")
    customer_id = db.Column(db.Integer, db.ForeignKey("customers.id", ondelete="SET NULL"), nullable=True, index=True)
    # "import" (eindeutige VN beim Listenimport) | "manual"
    link_basis = db.Column(db.String(20), nullable=True)
    linked_at = db.Column(db.DateTime, nullable=True)
    linked_by_user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_by_user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=_now)

    customer = db.relationship("Customer")
    created_by = db.relationship("User", foreign_keys=[created_by_user_id])
    linked_by = db.relationship("User", foreign_keys=[linked_by_user_id])
    notes = db.relationship("PendingCaseNote", back_populates="case", order_by="PendingCaseNote.created_at", cascade="all, delete-orphan")

    @property
    def is_open(self) -> bool:
        return self.status == "open"


class PendingCaseNote(TenantScopedMixin, db.Model):
    __tablename__ = "pending_case_notes"
    __table_args__ = (db.UniqueConstraint("tenant_id", "created_by_user_id", "client_ref", name="uq_pending_case_notes_client_ref"),)

    id = db.Column(db.Integer, primary_key=True)
    case_id = db.Column(db.Integer, db.ForeignKey("pending_cases.id", ondelete="CASCADE"), nullable=False, index=True)
    text = db.Column(db.Text, nullable=False)
    created_by_user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=_now)
    client_ref = db.Column(db.String(64), nullable=True)
    # Nach dem Verknuepfen: die uebernommene Kundennotiz (verhindert Kopien bei erneutem Import).
    customer_note_id = db.Column(db.Integer, db.ForeignKey("customer_notes.id", ondelete="SET NULL"), nullable=True)

    case = db.relationship("PendingCase", back_populates="notes")
    created_by = db.relationship("User")
