from datetime import datetime, timezone

from app.extensions import db
from app.tenancy import TenantScopedMixin


class Customer(TenantScopedMixin, db.Model):
    __tablename__ = "customers"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(255), nullable=False, index=True)
    address = db.Column(db.String(255))
    city = db.Column(db.String(120), index=True)
    postal_code = db.Column(db.String(20), index=True)
    date_of_birth = db.Column(db.Date, nullable=True)
    email = db.Column(db.String(255), nullable=True)
    phone = db.Column(db.String(50), nullable=True)

    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = db.Column(
        db.DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    # M11: einmalig beim ersten Anlegen gesetzt (siehe find_or_create_customer), danach nie
    # ueberschrieben - "Mein Bestand" filtert darueber.
    assigned_user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True, index=True)

    documents = db.relationship("Document", back_populates="customer")
    document_customers = db.relationship("DocumentCustomer", back_populates="customer")
    recommendations = db.relationship("Recommendation", back_populates="customer")
    tasks = db.relationship("Task", back_populates="customer")
    timeline_events = db.relationship(
        "CustomerTimelineEvent", back_populates="customer", cascade="all, delete-orphan"
    )
    assigned_user = db.relationship("User", foreign_keys=[assigned_user_id])

    def __repr__(self):
        return f"<Customer {self.id} {self.name!r}>"
