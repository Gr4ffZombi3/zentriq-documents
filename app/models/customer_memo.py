from datetime import datetime, timezone

from app.extensions import db
from app.tenancy import TenantScopedMixin


class CustomerMemo(TenantScopedMixin, db.Model):
    """Memo-Transkription, die einem Zentriq-Kunden zugeordnet wurde.

    Gespeichert wird nur das Transkript einer Sprachnachricht, die eindeutig erkannt oder vom
    Benutzer ausdruecklich einem Kunden zugeordnet wurde - nie die Audiodatei und nie Memos
    ohne Kundenzuordnung (app/services/memo_customers.py)."""

    __tablename__ = "customer_memos"
    __table_args__ = (
        # Dasselbe Transkript wird einem Kunden nur einmal zugeordnet (wiederholtes Absenden).
        db.UniqueConstraint("tenant_id", "customer_id", "transcript_sha256", name="uq_customer_memos_transcript"),
    )

    id = db.Column(db.Integer, primary_key=True)
    customer_id = db.Column(
        db.Integer, db.ForeignKey("customers.id", ondelete="CASCADE"), nullable=False, index=True
    )
    transcript = db.Column(db.Text, nullable=False)
    transcript_sha256 = db.Column(db.String(64), nullable=False)
    # Grundlage der Zuordnung: "customer_number" | "phone" (automatisch, eindeutig),
    # "selected" (vom Benutzer gewaehlt) | "new_customer" (Kunde aus dem Memo angelegt).
    matched_by = db.Column(db.String(20), nullable=False)
    created_by_user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=lambda: datetime.now(timezone.utc), index=True)

    customer = db.relationship("Customer", back_populates="memos")
    created_by = db.relationship("User")

    def __repr__(self):
        return f"<CustomerMemo {self.id} customer={self.customer_id}>"
