from datetime import datetime, timedelta, timezone

from sqlalchemy import func

from app.extensions import db
from app.models import Document, Recommendation
from app.models.enums import PRIORITY_ORDER, DocStatus, RecommendationStatus


def get_dashboard_stats() -> dict:
    total_documents = Document.query.count()

    since = datetime.now(timezone.utc) - timedelta(hours=24)
    new_documents = Document.query.filter(Document.uploaded_at >= since).count()

    done_count = Document.query.filter_by(status=DocStatus.DONE).count()
    failed_count = Document.query.filter_by(status=DocStatus.FAILED).count()

    doc_type_counts = dict(
        db.session.query(Document.doc_type, func.count(Document.id))
        .filter(Document.doc_type.isnot(None))
        .group_by(Document.doc_type)
        .all()
    )

    return {
        "total_documents": total_documents,
        "new_documents": new_documents,
        "done_count": done_count,
        "failed_count": failed_count,
        "doc_type_counts": doc_type_counts,
    }


def get_open_recommendations(limit: int = 10) -> list[Recommendation]:
    recommendations = Recommendation.query.filter_by(status=RecommendationStatus.OPEN).all()
    recommendations.sort(key=lambda r: (PRIORITY_ORDER[r.priority], r.created_at), reverse=True)
    return recommendations[:limit]
