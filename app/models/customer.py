from datetime import datetime, timezone

from sqlalchemy import event

from app.extensions import db
from app.tenancy import TenantScopedMixin
from app.utils.customer_keys import customer_keys


class Customer(TenantScopedMixin, db.Model):
    __tablename__ = "customers"
    __table_args__ = (
        db.Index("ix_customers_tenant_name_key", "tenant_id", "name_key"),
        db.Index("ix_customers_tenant_phone_key", "tenant_id", "phone_key"),
        db.Index("ix_customers_tenant_customer_number_key", "tenant_id", "customer_number_key"),
    )

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(255), nullable=False, index=True)
    address = db.Column(db.String(255))
    city = db.Column(db.String(120), index=True)
    postal_code = db.Column(db.String(20), index=True)
    date_of_birth = db.Column(db.Date, nullable=True)
    email = db.Column(db.String(255), nullable=True)
    phone = db.Column(db.String(50), nullable=True)
    # Kundennummer des Bueros (optional). Wird nur angezeigt/abgeglichen, wenn hinterlegt.
    customer_number = db.Column(db.String(50), nullable=True, index=True)
    # Vermittlernummer aus der zuletzt importierten Leipziger Liste (Anzeigeform "08/4205-M").
    broker_number = db.Column(db.String(50), nullable=True)

    # Datenherkunft (app/services/customer_sources.py): woher der Datensatz stammt und - je
    # Feld - woher der aktuelle Wert kommt, z. B. {"phone": "MEMO"}. Keine Versionierung.
    source = db.Column(db.String(20), nullable=True)
    field_sources = db.Column(db.JSON, nullable=True)

    # Vergleichsschluessel (app/utils/customer_keys.py), automatisch gepflegt (siehe unten):
    # Dubletten-Erkennung und Memo-Kundenerkennung laufen ueber diese Indizes.
    name_key = db.Column(db.String(255), nullable=True)
    phone_key = db.Column(db.String(20), nullable=True)
    customer_number_key = db.Column(db.String(50), nullable=True)

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
    memos = db.relationship("CustomerMemo", back_populates="customer", order_by="CustomerMemo.created_at.desc()")

    def __repr__(self):
        return f"<Customer {self.id} {self.name!r}>"


@event.listens_for(Customer, "before_insert")
@event.listens_for(Customer, "before_update")
def _refresh_customer_keys(mapper, connection, customer):
    for column, value in customer_keys(customer.name, customer.phone, customer.customer_number).items():
        if getattr(customer, column) != value:
            setattr(customer, column, value)
