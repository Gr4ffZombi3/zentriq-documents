from datetime import datetime, timezone

from app.extensions import db
from app.models.enums import Priority, RecommendationStatus, RecommendationType
from app.tenancy import TenantScopedMixin


class Recommendation(TenantScopedMixin, db.Model):
    __tablename__ = "recommendations"

    id = db.Column(db.Integer, primary_key=True)
    document_id = db.Column(db.Integer, db.ForeignKey("documents.id"), nullable=False, index=True)
    customer_id = db.Column(db.Integer, db.ForeignKey("customers.id"), nullable=True, index=True)

    type = db.Column(db.Enum(RecommendationType), nullable=False)
    label = db.Column(db.String(255), nullable=False)
    priority = db.Column(db.Enum(Priority), nullable=False, default=Priority.MEDIUM, index=True)
    status = db.Column(
        db.Enum(RecommendationStatus), nullable=False, default=RecommendationStatus.OPEN, index=True
    )

    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    resolved_at = db.Column(db.DateTime, nullable=True)

    # M12: generierte Begruendung (Template ueber echte extrahierte Werte, kein GPT-Call).
    explanation = db.Column(db.Text, nullable=True)

    document = db.relationship("Document", back_populates="recommendations")
    customer = db.relationship("Customer", back_populates="recommendations")

    def __repr__(self):
        return f"<Recommendation {self.id} {self.type} status={self.status}>"
