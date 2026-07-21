"""Abfrageschicht fuer Leipziger-Listen-Auswertungen.

Die Seite /potenziale arbeitet dokumentzentriert: Auswahl eines bereits hochgeladenen
Leipziger-Liste-Dokuments, kompakte Kennzahlen und eine Tabelle mit den extrahierten
Kundenzeilen. Alle Daten stammen ausschliesslich aus persistierten Analyseergebnissen
(`DocumentCustomer.row_data` + `field_confidence`) und nicht aus erneuten KI-Aufrufen.
"""

from __future__ import annotations

from datetime import date

from sqlalchemy.orm import joinedload, selectinload

from app.models import Document, DocumentCustomer
from app.models.enums import DocType, ListScope, PotentialCategory
from app.services.analysis.potential_classification import classify_row, explain_category

STATUS_FILTER_OPTIONS = [
    ("alle", "Alle"),
    ("angebote", "Angebote"),
    ("neugeschaeft", "Neugeschaeft"),
    ("fahrzeugwechsel", "Fahrzeugwechsel"),
    ("abgeschlossen", "Abgeschlossen"),
    ("ohne_beginn", "Ohne Beginn"),
    ("storno", "Storno"),
    ("unklar", "Unklar"),
]

STATUS_PRESENTATION = {
    "angebot": ("Angebot", "warning", "Angebot offen"),
    "neugeschaeft": ("Neugeschaeft", "info", "Neugeschaeft erkannt"),
    "fahrzeugwechsel": ("Fahrzeugwechsel", "info", "Fahrzeugwechsel erkannt"),
    "abgeschlossen": ("Abgeschlossen", "success", "Beginn vorhanden"),
    "storno": ("Storno", "danger", "Storno erkannt"),
    "unklar": ("Unklar", "muted", "Unklare Zuordnung"),
}

RELIABLE_HAS_ANTRAG_FIELDS = {"has_antrag"}
UNCERTAINTY_FIELDS = {
    "contract_number",
    "product_line",
    "broker_number",
    "contract_start_date",
    "is_angebot",
    "is_neugeschaeft",
    "is_fahrzeugwechsel",
    "is_storno",
    "has_antrag",
}


def _base_query(document_id: int | None, list_scope: ListScope | None, date_from: date | None, date_to: date | None):
    query = (
        DocumentCustomer.query.options(
            joinedload(DocumentCustomer.customer),
            joinedload(DocumentCustomer.document),
        )
        .join(Document, DocumentCustomer.document_id == Document.id)
        .filter(Document.doc_type == DocType.LEIPZIGER_LISTE)
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


def get_leipziger_documents() -> list[Document]:
    return (
        Document.query.options(selectinload(Document.document_customers))
        .filter(Document.doc_type == DocType.LEIPZIGER_LISTE)
        .order_by(Document.uploaded_at.desc())
        .all()
    )


def get_leipziger_document_options() -> list[dict]:
    options = []
    for document in get_leipziger_documents():
        row_count = sum(len(doc_customer.row_data or []) for doc_customer in document.document_customers)
        options.append(
            {
                "id": document.id,
                "name": document.original_filename,
                "uploaded_at_label": document.uploaded_at.strftime("%d.%m.%Y") if document.uploaded_at else "-",
                "status": document.status,
                "list_type_label": _document_list_type_label(document),
                "row_count": row_count,
            }
        )
    return options


def build_row_view(doc_customer: DocumentCustomer, row: dict, confidence: dict | None = None) -> dict:
    confidence = confidence or {}
    status_key = _row_status_key(row)
    status_label, status_variant, result_label = STATUS_PRESENTATION[status_key]
    is_uncertain = _row_is_uncertain(confidence)
    start_date = row.get("contract_start_date")
    broker_number = (row.get("broker_number") or "").strip() if isinstance(row.get("broker_number"), str) else None

    return {
        "document_id": doc_customer.document_id,
        "document_name": doc_customer.document.original_filename,
        "document_uploaded_at": doc_customer.document.uploaded_at,
        "customer_id": doc_customer.customer_id,
        "customer_name": doc_customer.customer.name if doc_customer.customer else "Unbekannter Kunde",
        "contract_number": row.get("contract_number") or "-",
        "status_key": status_key,
        "status_label": status_label,
        "status_variant": status_variant,
        "product_line": row.get("product_line") or "-",
        "start_date_label": _format_date(start_date),
        "broker_number": broker_number or "-",
        "result_label": result_label,
        "safety_label": "Unklar" if is_uncertain else "Sicher",
        "safety_variant": "muted" if is_uncertain else "success",
        "reason": explain_category(row, classify_row(row)),
        "is_uncertain": is_uncertain,
        "raw_row": row,
        "confidence": confidence,
    }


def build_document_analysis(
    *,
    document_id: int | None,
    status_filter: str = "alle",
    current_broker_number: str | None = None,
) -> dict:
    options = get_leipziger_document_options()
    selected_document_id = document_id or (options[0]["id"] if options else None)
    if selected_document_id is None:
        return {
            "document_options": options,
            "selected_document": None,
            "summary": _empty_summary(),
            "rows": [],
            "status_filter": status_filter,
            "status_filters": STATUS_FILTER_OPTIONS,
        }

    document = (
        Document.query.options(
            selectinload(Document.document_customers).joinedload(DocumentCustomer.customer)
        )
        .filter(Document.id == selected_document_id, Document.doc_type == DocType.LEIPZIGER_LISTE)
        .first()
    )
    if document is None:
        return {
            "document_options": options,
            "selected_document": None,
            "summary": _empty_summary(),
            "rows": [],
            "status_filter": status_filter,
            "status_filters": STATUS_FILTER_OPTIONS,
        }

    rows = []
    reliable_ohne_antrag = 0
    for doc_customer in document.document_customers:
        confidence_rows = doc_customer.field_confidence or []
        for index, row in enumerate(doc_customer.row_data or []):
            confidence = confidence_rows[index] if index < len(confidence_rows) else {}
            row_view = build_row_view(doc_customer, row, confidence)
            rows.append(row_view)
            if _is_reliably_without_antrag(row, confidence):
                reliable_ohne_antrag += 1

    rows.sort(
        key=lambda item: (
            item["document_uploaded_at"].timestamp() if item["document_uploaded_at"] else 0,
            item["customer_name"].lower(),
        ),
        reverse=True,
    )
    filtered_rows = [row for row in rows if _matches_status_filter(row, status_filter)]

    summary = {
        "total_records": len(rows),
        "abgeschlossen": sum(1 for row in rows if row["status_key"] == "abgeschlossen"),
        "angebote": sum(1 for row in rows if bool(row["raw_row"].get("is_angebot"))),
        "offene_vorgaenge": sum(
            1 for row in rows if row["status_key"] not in {"abgeschlossen", "storno"}
        ),
        "ohne_beginn": sum(1 for row in rows if not row["raw_row"].get("contract_start_date")),
        "stornos": sum(1 for row in rows if bool(row["raw_row"].get("is_storno"))),
        "ohne_antrag": reliable_ohne_antrag,
    }

    selected_document = {
        "id": document.id,
        "name": document.original_filename,
        "list_type_label": _document_list_type_label(document),
        "uploaded_at_label": document.uploaded_at.strftime("%d.%m.%Y %H:%M") if document.uploaded_at else "-",
        "vm_number_label": _document_broker_label(rows, current_broker_number),
        "row_count": len(rows),
        "status": document.status,
        "show_ohne_antrag": reliable_ohne_antrag > 0,
        "raw_json": document.raw_json,
    }

    return {
        "document_options": options,
        "selected_document": selected_document,
        "summary": summary,
        "rows": filtered_rows,
        "status_filter": status_filter,
        "status_filters": STATUS_FILTER_OPTIONS,
    }


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
        confidence_rows = doc_customer.field_confidence or []
        for index, row in enumerate(doc_customer.row_data or []):
            confidence = confidence_rows[index] if index < len(confidence_rows) else {}
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
            if _is_reliably_without_antrag(row, confidence):
                counters["ohne_antrag"] += 1

    counters["offene_vorgaenge"] = counters["total_records"] - counters["abgeschlossen"] - counters["stornos"]
    return counters


def _document_broker_label(rows: list[dict], current_broker_number: str | None) -> str:
    broker_numbers = sorted({row["broker_number"] for row in rows if row["broker_number"] and row["broker_number"] != "-"})
    if not broker_numbers:
        return "-"
    if len(broker_numbers) == 1:
        return broker_numbers[0]
    if current_broker_number and current_broker_number in broker_numbers:
        return f"{current_broker_number} + weitere"
    return "Mehrere"


def _document_list_type_label(document: Document) -> str:
    if document.list_type is not None:
        return document.list_type.label
    if document.list_scope == ListScope.OWN:
        return "Eigene Leipziger Liste"
    if document.list_scope == ListScope.GESCHAEFTSSTELLE:
        return "GS-Liste"
    return "Nicht angegeben"


def _row_status_key(row: dict) -> str:
    if row.get("is_storno"):
        return "storno"
    if row.get("contract_start_date"):
        return "abgeschlossen"
    if row.get("is_fahrzeugwechsel"):
        return "fahrzeugwechsel"
    if row.get("is_neugeschaeft"):
        return "neugeschaeft"
    if row.get("is_angebot"):
        return "angebot"
    return "unklar"


def _matches_status_filter(row_view: dict, status_filter: str) -> bool:
    if status_filter == "alle":
        return True
    row = row_view["raw_row"]
    if status_filter == "angebote":
        return bool(row.get("is_angebot"))
    if status_filter == "neugeschaeft":
        return bool(row.get("is_neugeschaeft"))
    if status_filter == "fahrzeugwechsel":
        return bool(row.get("is_fahrzeugwechsel"))
    if status_filter == "abgeschlossen":
        return bool(row.get("contract_start_date"))
    if status_filter == "ohne_beginn":
        return not row.get("contract_start_date")
    if status_filter == "storno":
        return bool(row.get("is_storno"))
    if status_filter == "unklar":
        return row_view["is_uncertain"] or row_view["status_key"] == "unklar"
    return True


def _row_is_uncertain(confidence: dict) -> bool:
    return any(_confidence_uncertain(confidence, field_name) for field_name in UNCERTAINTY_FIELDS)


def _is_reliably_without_antrag(row: dict, confidence: dict) -> bool:
    if row.get("has_antrag") is not False:
        return False
    return not any(_confidence_uncertain(confidence, field_name) for field_name in RELIABLE_HAS_ANTRAG_FIELDS)


def _confidence_uncertain(confidence: dict, field_name: str) -> bool:
    if not isinstance(confidence, dict):
        return False
    field_info = confidence.get(field_name)
    return bool(isinstance(field_info, dict) and field_info.get("uncertain"))


def _format_date(value) -> str:
    if isinstance(value, date):
        return value.strftime("%d.%m.%Y")
    if isinstance(value, str):
        try:
            return date.fromisoformat(value).strftime("%d.%m.%Y")
        except ValueError:
            return "-"
    return "-"


def _empty_summary() -> dict:
    return {
        "total_records": 0,
        "abgeschlossen": 0,
        "angebote": 0,
        "offene_vorgaenge": 0,
        "ohne_beginn": 0,
        "stornos": 0,
        "ohne_antrag": 0,
    }
