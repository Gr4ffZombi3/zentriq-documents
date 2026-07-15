from datetime import datetime, timezone

from app.extensions import db
from app.models.enums import ListChangeType
from app.tenancy import TenantScopedMixin


class ListComparison(TenantScopedMixin, db.Model):
    """Vergleichslauf zwischen einer neu hochgeladenen Leipziger Liste und der zuletzt
    verarbeiteten Leipziger Liste desselben Tenants."""

    __tablename__ = "list_comparisons"

    id = db.Column(db.Integer, primary_key=True)
    document_id = db.Column(db.Integer, db.ForeignKey("documents.id"), nullable=False, index=True)
    previous_document_id = db.Column(db.Integer, db.ForeignKey("documents.id"), nullable=True)
    compared_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    new_customer_count = db.Column(db.Integer, nullable=False, default=0)
    new_contract_count = db.Column(db.Integer, nullable=False, default=0)
    new_offer_count = db.Column(db.Integer, nullable=False, default=0)
    status_change_count = db.Column(db.Integer, nullable=False, default=0)
    storno_count = db.Column(db.Integer, nullable=False, default=0)
    removed_customer_count = db.Column(db.Integer, nullable=False, default=0)
    new_product_line_count = db.Column(db.Integer, nullable=False, default=0)

    document = db.relationship("Document", foreign_keys=[document_id])
    previous_document = db.relationship("Document", foreign_keys=[previous_document_id])
    entries = db.relationship(
        "ListComparisonEntry", back_populates="list_comparison", cascade="all, delete-orphan"
    )

    def __repr__(self):
        return f"<ListComparison {self.id} document_id={self.document_id}>"


class ListComparisonEntry(TenantScopedMixin, db.Model):
    __tablename__ = "list_comparison_entries"

    id = db.Column(db.Integer, primary_key=True)
    list_comparison_id = db.Column(
        db.Integer, db.ForeignKey("list_comparisons.id"), nullable=False, index=True
    )
    customer_id = db.Column(db.Integer, db.ForeignKey("customers.id"), nullable=False, index=True)
    change_type = db.Column(db.Enum(ListChangeType), nullable=False)
    details = db.Column(db.JSON, nullable=True)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    list_comparison = db.relationship("ListComparison", back_populates="entries")
    customer = db.relationship("Customer")

    def __repr__(self):
        return f"<ListComparisonEntry {self.id} {self.change_type}>"
