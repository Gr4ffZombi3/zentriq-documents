"""Lesende Uebersicht fuer den Bereich Leipziger Liste. Aggregiert ausschliesslich bereits
vorhandene Daten (Dokumente, ListComparison, Kunden, Aufgaben) - keine Analyse-Logik."""

from sqlalchemy import or_

from app.models import Customer, Document, ListComparison, Task
from app.models.enums import ComparisonKind, DocStatus, DocType, TaskStatus
from app.services.document_progress import is_document_active_status

CHANGE_COUNTERS = (
    ("new_customer_count", "Neue Kunden"),
    ("new_contract_count", "Neue Verträge"),
    ("new_offer_count", "Angebote"),
    ("status_change_count", "Statusänderungen"),
    ("storno_count", "Storno"),
    ("removed_customer_count", "Entfernte Kunden"),
    ("new_product_line_count", "Neue Produktsparten"),
)


def _latest_comparison(kind: ComparisonKind) -> ListComparison | None:
    return (
        ListComparison.query.filter_by(comparison_kind=kind)
        .order_by(ListComparison.compared_at.desc(), ListComparison.id.desc())
        .first()
    )


def _comparison_view(comparison: ListComparison | None) -> dict | None:
    if comparison is None:
        return None
    counters = [(label, getattr(comparison, attr) or 0) for attr, label in CHANGE_COUNTERS]
    return {"comparison": comparison, "counters": counters, "total": sum(value for _, value in counters)}


def build_leipziger_overview(recent_limit: int = 8) -> dict:
    # Frisch hochgeladene Listen haben noch keinen doc_type (wird erst bei der Verarbeitung
    # gesetzt), aber immer einen list_type aus dem Upload-Formular.
    lists_query = Document.query.filter(
        or_(Document.doc_type == DocType.LEIPZIGER_LISTE, Document.list_type.isnot(None))
    )
    recent_lists = lists_query.order_by(Document.uploaded_at.desc(), Document.id.desc()).limit(recent_limit).all()
    active_statuses = [status for status in DocStatus if is_document_active_status(status.value)]
    return {
        "recent_lists": recent_lists,
        "list_count": lists_query.count(),
        "processing_count": lists_query.filter(Document.status.in_(active_statuses)).count(),
        "failed_count": lists_query.filter(Document.status == DocStatus.FAILED).count(),
        "customer_count": Customer.query.count(),
        "open_task_count": Task.query.filter_by(status=TaskStatus.OPEN).count(),
        "temporal": _comparison_view(_latest_comparison(ComparisonKind.TEMPORAL)),
        "own_vs_gs": _comparison_view(_latest_comparison(ComparisonKind.OWN_VS_GS)),
    }
