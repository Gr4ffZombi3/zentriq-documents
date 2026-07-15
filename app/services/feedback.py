"""Nutzerbewertung (Daumen hoch/runter) einer Aufgabe/Empfehlung. Bewusst nur Datenerfassung
+ Auswertung in diesem Meilenstein (siehe app/models/feedback.py) - fliesst nicht automatisch
in Prompts oder Regelgewichte zurueck."""

from app.extensions import db
from app.models import RecommendationFeedback, Task
from app.models.enums import FeedbackRating


def record_feedback(task: Task, user_id: int, rating: FeedbackRating) -> RecommendationFeedback:
    feedback = RecommendationFeedback(
        tenant_id=task.tenant_id, task_id=task.id, rated_by_user_id=user_id, rating=rating
    )
    db.session.add(feedback)
    db.session.commit()
    return feedback


def get_accuracy_by_type() -> dict[str, dict]:
    """{task_type.value: {"up": n, "down": n, "accuracy_rate": up/(up+down) oder None}},
    automatisch tenant-gescoped ueber die Task-/RecommendationFeedback-Abfrage."""
    stats: dict[str, dict] = {}
    for feedback in RecommendationFeedback.query.all():
        task_type = feedback.task.type.value
        entry = stats.setdefault(task_type, {"up": 0, "down": 0})
        entry["up" if feedback.rating == FeedbackRating.UP else "down"] += 1

    for entry in stats.values():
        total = entry["up"] + entry["down"]
        entry["accuracy_rate"] = round(entry["up"] / total, 2) if total else None

    return stats
