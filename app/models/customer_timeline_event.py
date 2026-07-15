from datetime import datetime, timezone

from app.extensions import db
from app.models.enums import TimelineEventType
from app.tenancy import TenantScopedMixin


class CustomerTimelineEvent(TenantScopedMixin, db.Model):
    """Append-only Chronologie-Eintrag pro Kunde. `label` wird stets serverseitig aus
    einem festen Template gebaut - nie aus freiem Nutzertext -, damit die Chronologie
    konsistent bleibt."""

    __tablename__ = "customer_timeline_events"

    id = db.Column(db.Integer, primary_key=True)
    customer_id = db.Column(db.Integer, db.ForeignKey("customers.id"), nullable=False, index=True)
    document_id = db.Column(db.Integer, db.ForeignKey("documents.id"), nullable=True)
    task_id = db.Column(db.Integer, db.ForeignKey("tasks.id"), nullable=True)

    event_type = db.Column(db.Enum(TimelineEventType), nullable=False)
    label = db.Column(db.String(255), nullable=False)
    occurred_at = db.Column(db.DateTime, nullable=False, index=True)
    extra_data = db.Column(db.JSON, nullable=True)

    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    customer = db.relationship("Customer", back_populates="timeline_events")
    document = db.relationship("Document")
    task = db.relationship("Task")

    def __repr__(self):
        return f"<CustomerTimelineEvent {self.id} {self.event_type} customer_id={self.customer_id}>"
