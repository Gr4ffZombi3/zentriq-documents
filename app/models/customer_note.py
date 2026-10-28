from datetime import datetime, timezone

from app.extensions import db
from app.tenancy import TenantScopedMixin


def utcnow():
    return datetime.now(timezone.utc)


class CustomerContact(TenantScopedMixin, db.Model):
    """Kontaktvorgang zu einem Kunden ("Offene Rueckrufe").

    Entsteht nur aus einer Notiz mit ausdruecklich offenem Kontakt ("nicht erreicht", "bitte
    zurueckrufen", "Kunde ruft zurueck") oder ueber die Schnellaktion "Nicht erreicht" - nie aus
    einer normalen Notiz. Weitere Kontaktversuche zum selben Anlass ergaenzen den Vorgang
    (attempts, outcome, updated_*), statt einen neuen anzulegen. "Erledigt" schliesst genau diesen
    Vorgang (app/services/customer_notes.py)."""

    __tablename__ = "customer_contacts"
    __table_args__ = (db.Index("ix_customer_contacts_tenant_status_updated", "tenant_id", "status", "updated_at"),)

    id = db.Column(db.Integer, primary_key=True)
    customer_id = db.Column(db.Integer, db.ForeignKey("customers.id", ondelete="CASCADE"), nullable=False, index=True)
    # Kurzer Anlass ("HUK24 Wechsel"); leer, wenn die Notiz keinen nennt.
    topic = db.Column(db.String(200), nullable=True)
    # "open" | "done"
    status = db.Column(db.String(20), nullable=False, default="open")
    # Letzter Kontaktstand: "not_reached" | "callback_requested" | "customer_calls_back"
    outcome = db.Column(db.String(30), nullable=False)
    attempts = db.Column(db.Integer, nullable=False, default=1)

    created_by_user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    updated_by_user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    updated_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    closed_by_user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    closed_at = db.Column(db.DateTime, nullable=True)

    customer = db.relationship("Customer")
    created_by = db.relationship("User", foreign_keys=[created_by_user_id])
    updated_by = db.relationship("User", foreign_keys=[updated_by_user_id])
    closed_by = db.relationship("User", foreign_keys=[closed_by_user_id])

    def __repr__(self):
        return f"<CustomerContact {self.id} customer={self.customer_id} {self.status}>"


class CustomerNote(TenantScopedMixin, db.Model):
    """Gemeinsame Kundennotiz des Bueros (ZentriqAI-Eingabefeld bzw. Schnellaktion der Kundenkarte).

    Notizen werden nie geaendert oder geloescht; Statusaenderungen eines Kontaktvorgangs werden
    ebenfalls als Notiz (event) mit Verfasser und Zeitpunkt festgehalten."""

    __tablename__ = "customer_notes"
    __table_args__ = (
        # Doppelklick / erneut uebermittelte Anfrage: dieselbe client_ref speichert nur einmal.
        db.UniqueConstraint("tenant_id", "created_by_user_id", "client_ref", name="uq_customer_notes_client_ref"),
        db.Index("ix_customer_notes_customer_created", "customer_id", "created_at"),
    )

    id = db.Column(db.Integer, primary_key=True)
    customer_id = db.Column(db.Integer, db.ForeignKey("customers.id", ondelete="CASCADE"), nullable=False)
    contact_id = db.Column(db.Integer, db.ForeignKey("customer_contacts.id", ondelete="SET NULL"), nullable=True, index=True)
    text = db.Column(db.Text, nullable=False)
    # None: normale Notiz. Sonst Kontaktstand ("not_reached", "callback_requested",
    # "customer_calls_back") bzw. "done" (Vorgang abgeschlossen).
    event = db.Column(db.String(30), nullable=True)
    created_by_user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow, index=True)
    client_ref = db.Column(db.String(64), nullable=True)

    customer = db.relationship("Customer")
    contact = db.relationship("CustomerContact")
    created_by = db.relationship("User")

    def __repr__(self):
        return f"<CustomerNote {self.id} customer={self.customer_id}>"
