from app.extensions import db
from app.tenancy import TenantScopedMixin


class LeipzigerEntry(TenantScopedMixin, db.Model):
    """Ein Vorgang (eine Zeile) einer importierten Leipziger Liste in normalisierter Form.

    Abgeleitet aus DocumentCustomer.row_data (die JSON-Zeilen bleiben die Quelle der Anzeige).
    Die Tabelle dient ausschliesslich indizierten Abfragen: globale Suche, Zuordnung zu
    Mitarbeitern ueber die normalisierte Vermittlernummer und Importvergleich ueber die
    Vertragsnummer. Wird bei jedem (Neu-)Import eines Dokuments komplett neu geschrieben."""

    __tablename__ = "leipziger_entries"
    __table_args__ = (
        db.Index("ix_leipziger_entries_doc_broker", "tenant_id", "document_id", "broker_key"),
        db.Index("ix_leipziger_entries_doc_contract", "tenant_id", "document_id", "contract_key"),
        db.Index("ix_leipziger_entries_customer", "tenant_id", "customer_id"),
    )

    id = db.Column(db.Integer, primary_key=True)
    document_id = db.Column(
        db.Integer, db.ForeignKey("documents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # Zentriq-Kunde, dem der Vorgang beim Import zugeordnet wurde (ein Kunde, viele Vorgaenge).
    customer_id = db.Column(db.Integer, db.ForeignKey("customers.id", ondelete="SET NULL"), nullable=True)
    # Reihenfolge wie in der PDF.
    position = db.Column(db.Integer, nullable=False, default=0)

    contract_number = db.Column(db.String(100), nullable=True)
    # Nur Ziffern/Grossbuchstaben ("KH 47-11" -> "KH4711") - Vergleich und Suche.
    contract_key = db.Column(db.String(100), nullable=True)
    customer_name = db.Column(db.String(255), nullable=True)
    customer_key = db.Column(db.String(255), nullable=True)
    broker_number = db.Column(db.String(50), nullable=True)
    broker_key = db.Column(db.String(50), nullable=True)
    status_code = db.Column(db.String(20), nullable=True)
    product_line = db.Column(db.String(100), nullable=True)
    start_date = db.Column(db.String(10), nullable=True)

    document = db.relationship("Document", back_populates="leipziger_entries")

    def __repr__(self):
        return f"<LeipzigerEntry {self.id} {self.contract_number!r}>"
