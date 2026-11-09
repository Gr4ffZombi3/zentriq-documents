from datetime import datetime, timezone

from sqlalchemy import event

from app.extensions import db
from app.tenancy import TenantScopedMixin
from app.utils.customer_keys import customer_keys, normalize_phone


class Customer(TenantScopedMixin, db.Model):
    __tablename__ = "customers"
    __table_args__ = (
        db.Index("ix_customers_tenant_name_key", "tenant_id", "name_key"),
        db.Index("ix_customers_tenant_phone_key", "tenant_id", "phone_key"),
        db.Index("ix_customers_tenant_customer_number_key", "tenant_id", "customer_number_key"),
        db.Index("ix_customers_tenant_phone_landline_key", "tenant_id", "phone_landline_key"),
        db.Index("ix_customers_tenant_phone_private_key", "tenant_id", "phone_private_key"),
    )

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(255), nullable=False, index=True)
    address = db.Column(db.String(255))
    # Strukturierte Erfassung (optional, z. B. Kundenanlage im ZentriqAI-Feld). `name` bzw.
    # `address` bleiben die Anzeigefelder und werden dabei mitgepflegt ("Vorname Nachname",
    # "Strasse Hausnummer"); aeltere Datensaetze haben hier keine Werte.
    first_name = db.Column(db.String(120), nullable=True)
    last_name = db.Column(db.String(120), nullable=True)
    street = db.Column(db.String(200), nullable=True)
    house_number = db.Column(db.String(20), nullable=True)
    city = db.Column(db.String(120), index=True)
    postal_code = db.Column(db.String(20), index=True)
    date_of_birth = db.Column(db.Date, nullable=True)
    email = db.Column(db.String(255), nullable=True)
    # Bisherige Telefonnummer ohne Typ (aeltere Datensaetze, Memos, E-Mails) - bleibt erhalten.
    phone = db.Column(db.String(50), nullable=True)
    # Kundenanlage/-bearbeitung: getrennt nach Festnetz und Privattelefon (beide optional).
    phone_landline = db.Column(db.String(50), nullable=True)
    phone_private = db.Column(db.String(50), nullable=True)
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
    phone_landline_key = db.Column(db.String(20), nullable=True)
    phone_private_key = db.Column(db.String(20), nullable=True)

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

    @classmethod
    def phone_key_in(cls, keys):
        """SQL-Bedingung: eine der Nummern (bisherige, Festnetz, Privattelefon) passt."""
        keys = list(keys)
        return db.or_(cls.phone_key.in_(keys), cls.phone_landline_key.in_(keys), cls.phone_private_key.in_(keys))

    @property
    def phone_keys(self) -> set[str]:
        return {k for k in (self.phone_key, self.phone_landline_key, self.phone_private_key) if k}

    @property
    def phone_display(self) -> str | None:
        parts = []
        if self.phone_landline:
            parts.append(f"Festnetz {self.phone_landline}")
        if self.phone_private:
            parts.append(f"privat {self.phone_private}")
        if self.phone and normalize_phone(self.phone) not in {self.phone_landline_key, self.phone_private_key}:
            parts.append(f"Tel. {self.phone}" if parts else self.phone)
        return " · ".join(parts) or None

    def __repr__(self):
        return f"<Customer {self.id} {self.name!r}>"


@event.listens_for(Customer, "before_insert")
@event.listens_for(Customer, "before_update")
def _refresh_customer_keys(mapper, connection, customer):
    keys = customer_keys(customer.name, customer.phone, customer.customer_number)
    keys["phone_landline_key"] = normalize_phone(customer.phone_landline)
    keys["phone_private_key"] = normalize_phone(customer.phone_private)
    for column, value in keys.items():
        if getattr(customer, column) != value:
            setattr(customer, column, value)
