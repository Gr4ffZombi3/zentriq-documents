from datetime import datetime, timezone

from app.extensions import db
from app.models.enums import DocStatus, DocType, OcrEngine, Priority
from app.tenancy import TenantScopedMixin


class Document(TenantScopedMixin, db.Model):
    __tablename__ = "documents"

    id = db.Column(db.Integer, primary_key=True)

    # Datei / Identität
    filename = db.Column(db.String(255), nullable=False)
    original_filename = db.Column(db.String(255), nullable=False)
    file_path = db.Column(db.String(500), nullable=False)
    mime_type = db.Column(db.String(100), default="application/pdf")
    uploaded_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    processed_at = db.Column(db.DateTime, nullable=True)

    # Status
    doc_type = db.Column(db.Enum(DocType), nullable=True, index=True)
    status = db.Column(db.Enum(DocStatus), nullable=False, default=DocStatus.PENDING, index=True)
    error_message = db.Column(db.Text, nullable=True)
    retry_count = db.Column(db.Integer, nullable=False, default=0)

    # OCR-Metadaten
    ocr_engine_used = db.Column(db.Enum(OcrEngine), nullable=False, default=OcrEngine.NONE)
    ocr_confidence = db.Column(db.Float, nullable=True)

    # Rohdaten
    raw_text = db.Column(db.Text, nullable=True)
    raw_json = db.Column(db.JSON, nullable=True)

    # Typisierte Extraktionsspalten
    customer_id = db.Column(db.Integer, db.ForeignKey("customers.id"), nullable=True, index=True)
    vehicle = db.Column(db.String(255), nullable=True)
    license_plate = db.Column(db.String(20), nullable=True, index=True)
    insurer = db.Column(db.String(255), nullable=True)
    contract_number = db.Column(db.String(100), nullable=True)
    case_number = db.Column(db.String(100), nullable=True)
    broker = db.Column(db.String(255), nullable=True)
    contract_start_date = db.Column(db.Date, nullable=True)
    products = db.Column(db.JSON, nullable=True)
    special_notes = db.Column(db.Text, nullable=True)

    # Leipziger-Liste-Flags (echte Spalten fuer Suchbarkeit)
    is_neugeschaeft = db.Column(db.Boolean, nullable=True)
    is_fahrzeugwechsel = db.Column(db.Boolean, nullable=True)
    is_angebot = db.Column(db.Boolean, nullable=True)
    cross_sell_opportunity = db.Column(db.Boolean, nullable=True)
    has_multiple_products = db.Column(db.Boolean, nullable=True)
    is_storno = db.Column(db.Boolean, nullable=True)
    priority = db.Column(db.Enum(Priority), nullable=True, index=True)
    recommended_next_action = db.Column(db.String(255), nullable=True)

    extra_data = db.Column(db.JSON, nullable=True)

    # M11: wer den Upload ausgeloest hat (fuer "Mein Bestand"-Zuordnung); nullable, da
    # aeltere Dokumente und Celery-interne Erzeugung keinen User-Kontext haben.
    uploaded_by_user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True, index=True)

    customer = db.relationship("Customer", back_populates="documents")
    document_customers = db.relationship(
        "DocumentCustomer", back_populates="document", cascade="all, delete-orphan"
    )
    recommendations = db.relationship(
        "Recommendation", back_populates="document", cascade="all, delete-orphan"
    )
    tasks = db.relationship("Task", back_populates="document", cascade="all, delete-orphan")
    uploaded_by = db.relationship("User", foreign_keys=[uploaded_by_user_id])

    def __repr__(self):
        return f"<Document {self.id} {self.original_filename!r} status={self.status}>"


class DocumentCustomer(TenantScopedMixin, db.Model):
    """Verknuepft ein Document mit mehreren Kunden (z.B. Leipziger Liste)."""

    __tablename__ = "document_customers"
    __table_args__ = (
        db.UniqueConstraint("tenant_id", "document_id", "customer_id", name="uq_document_customer_tenant"),
    )

    id = db.Column(db.Integer, primary_key=True)
    document_id = db.Column(db.Integer, db.ForeignKey("documents.id"), nullable=False, index=True)
    customer_id = db.Column(db.Integer, db.ForeignKey("customers.id"), nullable=False, index=True)
    row_data = db.Column(db.JSON, nullable=True)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    document = db.relationship("Document", back_populates="document_customers")
    customer = db.relationship("Customer", back_populates="document_customers")
