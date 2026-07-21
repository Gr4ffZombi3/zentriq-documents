from __future__ import annotations

from datetime import date, datetime

from sqlalchemy.orm import joinedload

from app.models import Document, DocumentCustomer
from app.models.enums import DocType
from app.services.analysis.potential_classification import classify_row

CASE_LIMIT = 8
RECENT_DOCUMENT_LIMIT = 5


def build_dashboard_view(user) -> dict:
    row_entries = _load_leipziger_rows()
    preferred_broker_number = (getattr(user, "vermittlernummer", None) or "").strip() or None

    broker_matches = [
        entry for entry in row_entries if preferred_broker_number and entry["broker_number"] == preferred_broker_number
    ]
    scoped_rows = broker_matches or row_entries

    return {
        "headline": "Uebersicht",
        "subheadline": "Auswertung deiner Leipziger Listen",
        "scope_note": _build_scope_note(preferred_broker_number, broker_matches, row_entries),
        "has_data": bool(row_entries),
        "metrics": _build_metrics(scoped_rows),
        "cases": _build_cases(scoped_rows),
        "recent_documents": _build_recent_documents(),
    }


def _load_leipziger_rows() -> list[dict]:
    doc_customers = (
        DocumentCustomer.query.options(
            joinedload(DocumentCustomer.customer),
            joinedload(DocumentCustomer.document),
        )
        .join(Document, DocumentCustomer.document_id == Document.id)
        .filter(Document.doc_type == DocType.LEIPZIGER_LISTE)
        .order_by(Document.uploaded_at.desc(), DocumentCustomer.created_at.desc())
        .all()
    )

    row_entries: list[dict] = []
    for doc_customer in doc_customers:
        rows = doc_customer.row_data or []
        confidence_rows = doc_customer.field_confidence or []
        for index, row in enumerate(rows):
            confidence = confidence_rows[index] if index < len(confidence_rows) else {}
            row_entries.append(_serialize_row_entry(doc_customer, row, confidence))
    return row_entries


def _serialize_row_entry(doc_customer: DocumentCustomer, row: dict, confidence: dict) -> dict:
    document = doc_customer.document
    customer = doc_customer.customer
    category = classify_row(row)
    status_key = _status_key(category, confidence)
    start_date = _parse_date(row.get("contract_start_date"))

    return {
        "customer_id": customer.id if customer else None,
        "customer_name": customer.name if customer else "Unbekannter Kunde",
        "document_id": document.id,
        "document_name": document.original_filename,
        "document_uploaded_at": document.uploaded_at,
        "broker_number": _clean_text(row.get("broker_number")),
        "product_line": _product_label(row),
        "start_date": start_date,
        "start_date_label": start_date.strftime("%d.%m.%Y") if start_date else "-",
        "status_key": status_key,
        "status_label": _status_label(status_key),
        "status_variant": _status_variant(status_key),
        "result_label": _result_label(status_key, start_date),
        "category": category.value,
        "is_uncertain": _row_is_uncertain(category, confidence),
        "uncertainty_label": _uncertainty_label(category, confidence),
        "document_status": document.status,
    }


def _build_scope_note(preferred_broker_number: str | None, broker_matches: list[dict], row_entries: list[dict]) -> str:
    if preferred_broker_number and broker_matches:
        return f"Fokus auf Vermittlernummer {preferred_broker_number}."
    if preferred_broker_number and row_entries:
        return (
            f"Keine direkte Zuordnung fuer Vermittlernummer {preferred_broker_number} gefunden. "
            "Die Uebersicht zeigt daher alle Leipziger-Vorgaenge."
        )
    if row_entries:
        return "Die Uebersicht basiert auf allen gespeicherten Leipziger-Listen."
    return "Sobald Leipziger Listen analysiert wurden, erscheinen hier belegbare Vorgaenge."


def _build_metrics(row_entries: list[dict]) -> list[dict]:
    counters = {
        "application": 0,
        "offer": 0,
        "closed": 0,
        "follow_up": 0,
    }
    for entry in row_entries:
        status_key = entry["status_key"]
        if status_key == "application":
            counters["application"] += 1
        elif status_key == "offer":
            counters["offer"] += 1
        elif status_key == "closed":
            counters["closed"] += 1
        else:
            counters["follow_up"] += 1

    return [
        {"label": "Antraege eingereicht", "value": counters["application"], "tone": "info"},
        {"label": "Angebote offen", "value": counters["offer"], "tone": "warning"},
        {"label": "Beginn vorhanden", "value": counters["closed"], "tone": "success"},
        {"label": "Nacharbeit erforderlich", "value": counters["follow_up"], "tone": "danger"},
    ]


def _build_cases(row_entries: list[dict]) -> list[dict]:
    ranked_rows = sorted(row_entries, key=_case_sort_key)
    visible_rows = [entry for entry in ranked_rows if entry["status_key"] != "closed"] or ranked_rows
    cases = []
    for entry in visible_rows[:CASE_LIMIT]:
        case = dict(entry)
        case["customer_href"] = (
            f"/customers/{entry['customer_id']}" if entry["customer_id"] is not None else None
        )
        case["document_href"] = f"/documents/{entry['document_id']}"
        cases.append(case)
    return cases


def _build_recent_documents() -> list[dict]:
    documents = (
        Document.query.options(joinedload(Document.document_customers))
        .filter(Document.doc_type == DocType.LEIPZIGER_LISTE)
        .order_by(Document.uploaded_at.desc())
        .limit(RECENT_DOCUMENT_LIMIT)
        .all()
    )

    recent_documents = []
    for document in documents:
        row_count = sum(len(doc_customer.row_data or []) for doc_customer in document.document_customers)
        recent_documents.append(
            {
                "id": document.id,
                "name": document.original_filename,
                "uploaded_at_label": document.uploaded_at.strftime("%d.%m.%Y %H:%M") if document.uploaded_at else "-",
                "status": document.status,
                "recognized_count": row_count,
                "href": f"/documents/{document.id}",
            }
        )
    return recent_documents


def _case_sort_key(entry: dict) -> tuple:
    priority_order = {
        "follow_up": 0,
        "unclear": 1,
        "application": 2,
        "offer": 3,
        "closed": 4,
    }
    uploaded_at = entry["document_uploaded_at"]
    return (
        priority_order.get(entry["status_key"], 5),
        0 if entry["is_uncertain"] else 1,
        -(uploaded_at.timestamp() if uploaded_at else 0),
        entry["customer_name"].lower(),
    )


def _status_key(category, confidence: dict) -> str:
    if category.value == "abgeschlossen":
        return "closed"
    if category.value == "pruefen":
        return "application"
    if category.value == "nur_angebot":
        return "offer"
    if category.value == "storniert":
        return "follow_up"
    if _row_has_signal_uncertainty(confidence):
        return "follow_up"
    return "unclear"


def _status_label(status_key: str) -> str:
    return {
        "closed": "Abgeschlossen",
        "application": "Antrag eingereicht",
        "offer": "Angebot",
        "follow_up": "Nacharbeit noetig",
        "unclear": "Unklar",
    }[status_key]


def _status_variant(status_key: str) -> str:
    return {
        "closed": "success",
        "application": "info",
        "offer": "warning",
        "follow_up": "danger",
        "unclear": "muted",
    }[status_key]


def _result_label(status_key: str, start_date: date | None) -> str:
    if status_key == "closed":
        return start_date.strftime("%d.%m.%Y") if start_date else "Beginn vorhanden"
    if status_key == "application":
        return "Beginn noch offen"
    if status_key == "offer":
        return "Angebot offen"
    if status_key == "follow_up":
        return "Bitte pruefen"
    return "Angaben fehlen"


def _product_label(row: dict) -> str:
    product_line = _clean_text(row.get("product_line"))
    if product_line:
        return product_line
    products = [item.strip() for item in (row.get("products") or []) if isinstance(item, str) and item.strip()]
    if products:
        return ", ".join(products[:2])
    return "-"


def _clean_text(value) -> str | None:
    if isinstance(value, str):
        value = value.strip()
        return value or None
    return None


def _parse_date(value) -> date | None:
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError:
            return None
    return None


def _row_is_uncertain(category, confidence: dict) -> bool:
    relevant_fields = {
        "abgeschlossen": ("contract_start_date",),
        "pruefen": ("has_antrag",),
        "nur_angebot": ("is_angebot",),
        "storniert": ("is_storno",),
        "offener_vorgang": ("is_angebot", "has_antrag", "contract_start_date", "is_storno"),
    }
    return any(_confidence_uncertain(confidence, field_name) for field_name in relevant_fields.get(category.value, ()))


def _uncertainty_label(category, confidence: dict) -> str | None:
    if not _row_is_uncertain(category, confidence):
        return None
    if category.value == "abgeschlossen":
        return "Beginn unsicher erkannt"
    if category.value == "pruefen":
        return "Antragssignal unsicher"
    if category.value == "nur_angebot":
        return "Angebotssignal unsicher"
    if category.value == "storniert":
        return "Storno-Signal unsicher"
    return "Pruefung empfohlen"


def _row_has_signal_uncertainty(confidence: dict) -> bool:
    return any(
        _confidence_uncertain(confidence, field_name)
        for field_name in ("is_angebot", "has_antrag", "contract_start_date", "is_storno")
    )


def _confidence_uncertain(confidence: dict, field_name: str) -> bool:
    field_info = confidence.get(field_name) if isinstance(confidence, dict) else None
    return bool(isinstance(field_info, dict) and field_info.get("uncertain"))
