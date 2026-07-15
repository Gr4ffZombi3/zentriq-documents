from datetime import datetime, timezone

from app.extensions import db
from app.models.enums import Priority, TaskStatus, TaskType, WiedervorlageReason
from app.tenancy import TenantScopedMixin


class Task(TenantScopedMixin, db.Model):
    __tablename__ = "tasks"

    id = db.Column(db.Integer, primary_key=True)
    document_id = db.Column(db.Integer, db.ForeignKey("documents.id"), nullable=True, index=True)
    customer_id = db.Column(db.Integer, db.ForeignKey("customers.id"), nullable=True, index=True)
    recommendation_id = db.Column(db.Integer, db.ForeignKey("recommendations.id"), nullable=True, index=True)
    assigned_user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True, index=True)

    type = db.Column(db.Enum(TaskType), nullable=False)
    wiedervorlage_reason = db.Column(db.Enum(WiedervorlageReason), nullable=True)
    title = db.Column(db.String(255), nullable=False)
    priority = db.Column(db.Enum(Priority), nullable=False, default=Priority.MEDIUM, index=True)
    status = db.Column(db.Enum(TaskStatus), nullable=False, default=TaskStatus.OPEN, index=True)
    due_date = db.Column(db.Date, nullable=True, index=True)
    notes = db.Column(db.Text, nullable=True)

    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    resolved_at = db.Column(db.DateTime, nullable=True)

    document = db.relationship("Document", back_populates="tasks")
    customer = db.relationship("Customer", back_populates="tasks")
    recommendation = db.relationship("Recommendation")
    assigned_user = db.relationship("User")

    def __repr__(self):
        return f"<Task {self.id} {self.type} status={self.status}>"
