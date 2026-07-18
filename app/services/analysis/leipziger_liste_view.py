"""Abfrageschicht fuer die '/potenziale'-Seite (M13). Arbeitet auf DocumentCustomer.row_data
(ein Eintrag pro Kundenzeile innerhalb eines Leipziger-Liste-Dokuments), nicht auf Document-
Ebene wie das bestehende FilterSpec/search_documents() - Leipziger-Liste-Daten sind inhaerent
pro Zeile/pro Kunde, nicht pro Dokument. Tenant-Scoping erfolgt automatisch ueber die
DocumentCustomer/Document-Queries (TenantScopedMixin)."""

from datetime import date

from app.models import Document, DocumentCustomer
from app.models.enums import DocType, ListScope, PotentialCategory
from app.services.analysis.potential_classification import classify_row, explain_category


def _base_query(document_id: int | None, list_scope: ListScope | None, date_from: date | None, date_to: date | None):
    query = DocumentCustomer.query.join(Document, DocumentCustomer.document_id == Document.id).filter(
        Document.doc_type == DocType.LEIPZIGER_LISTE
    )
    if document_id is not None:
        query = query.filter(DocumentCustomer.document_id == document_id)
    if list_scope is not None:
        query = query.filter(Document.list_scope == list_scope)
    if date_from is not None:
        query = query.filter(Document.uploaded_at >= date_from)
    if date_to is not None:
        query = query.filter(Document.uploaded_at <= date_to)
    return query


def get_potential_records(
    *,
    category: PotentialCategory | None = None,
    include_closed: bool = False,
    product_line: str | None = None,
    broker_number: str | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    document_id: int | None = None,
    list_scope: ListScope | None = None,
) -> list[dict]:
    """Ein Ergebnis-Dict pro Kundenzeile, gefiltert. `include_closed=False` (Default) blendet
    ABGESCHLOSSEN aus - erledigte Datensaetze stehen wie gefordert standardmaessig nicht im
    Fokus, sind aber ueber include_closed=True weiterhin abrufbar."""
    records: list[dict] = []
    for doc_customer in _base_query(document_id, list_scope, date_from, date_to).all():
        document = doc_customer.document
        for row in doc_customer.row_data or []:
            row_category = classify_row(row)

            if category is not None and row_category != category:
                continue
            if not include_closed and category is None and row_category == PotentialCategory.ABGESCHLOSSEN:
                continue
            if product_line and row.get("product_line") != product_line:
                continue
            if broker_number and row.get("broker_number") != broker_number:
                continue

            records.append(
                {
                    "customer_id": doc_customer.customer_id,
                    "customer_name": doc_customer.customer.name if doc_customer.customer else None,
                    "product": ", ".join(row.get("products") or []),
                    "product_line": row.get("product_line"),
                    "broker_number": row.get("broker_number"),
                    "category": row_category,
                    "angebotsdatum": document.uploaded_at,
                    "reason": explain_category(row, row_category),
                    "document_id": document.id,
                }
            )
    return records


def get_analysis_summary(document: Document | None = None) -> dict:
    """Die 7 Kennzahlen aus "Analyseergebnis": Anzahl Datensaetze, Abschluesse, Angebote,
    offene Vorgaenge, Stornos, Vorgaenge ohne Beginn, Vorgaenge ohne Antrag."""
    counters = {
        "total_records": 0,
        "abgeschlossen": 0,
        "angebote": 0,
        "stornos": 0,
        "ohne_beginn": 0,
        "ohne_antrag": 0,
    }
    document_id = document.id if document is not None else None
    for doc_customer in _base_query(document_id, None, None, None).all():
        for row in doc_customer.row_data or []:
            counters["total_records"] += 1
            row_category = classify_row(row)
            if row_category == PotentialCategory.ABGESCHLOSSEN:
                counters["abgeschlossen"] += 1
            elif row_category == PotentialCategory.STORNIERT:
                counters["stornos"] += 1
            if row.get("is_angebot"):
                counters["angebote"] += 1
            if not row.get("contract_start_date"):
                counters["ohne_beginn"] += 1
            if not row.get("has_antrag"):
                counters["ohne_antrag"] += 1

    counters["offene_vorgaenge"] = counters["total_records"] - counters["abgeschlossen"] - counters["stornos"]
    return counters
