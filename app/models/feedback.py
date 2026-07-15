from datetime import datetime, timezone

from app.extensions import db
from app.models.enums import FeedbackRating
from app.tenancy import TenantScopedMixin


class RecommendationFeedback(TenantScopedMixin, db.Model):
    """Nutzerbewertung (Daumen hoch/runter) einer Aufgabe/Empfehlung. Bewusst nur
    Datenerfassung + Auswertung in M12 - fliesst nicht automatisch in Prompts/Regelgewichte
    zurueck (siehe docs/M12_COMPLETION_REPORT.md)."""

    __tablename__ = "recommendation_feedback"

    id = db.Column(db.Integer, primary_key=True)
    task_id = db.Column(db.Integer, db.ForeignKey("tasks.id"), nullable=False, index=True)
    rated_by_user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    rating = db.Column(db.Enum(FeedbackRating), nullable=False)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    task = db.relationship("Task", back_populates="feedback_entries")
    rated_by = db.relationship("User")

    def __repr__(self):
        return f"<RecommendationFeedback {self.id} task_id={self.task_id} rating={self.rating}>"
