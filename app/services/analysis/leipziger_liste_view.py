"""Abfrageschicht fuer Leipziger-Listen-Auswertungen.

Die Seite /potenziale arbeitet dokumentzentriert und nutzt ausschliesslich persistierte
Analyseergebnisse (`DocumentCustomer.row_data` + `field_confidence`). Fuer die UI werden
zwei Sichten aus denselben Daten gebaut:

- flache Vertragszeilen
- gruppierte Kundenansicht mit aufklappbaren Vertragsdetails
"""

from __future__ import annotations

from datetime import date

from sqlalchemy.orm import joinedload, selectinload

from app.models import Document, DocumentCustomer
from app.models.enums import DocType, ListScope, PotentialCategory
from app.services.customer_normalization import normalize_customer_name, normalize_postal_code
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
    "angebot": ("Angebot", "warning"),
    "neugeschaeft": ("Neugeschaeft", "info"),
    "fahrzeugwechsel": ("Fahrzeugwechsel", "info"),
    "storno": ("Storno", "danger"),
    "unklar": ("Unklar", "muted"),
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
        analysis_meta = _analysis_meta(document)
        row_count = analysis_meta.get("stored_row_count")
        if row_count is None:
            row_count = sum(len(doc_customer.row_data or []) for doc_customer in document.document_customers)
        options.append(
            {
                "id": document.id,
                "name": document.original_filename,
                "uploaded_at_label": document.uploaded_at.strftime("%d.%m.%Y") if document.uploaded_at else "-",
                "status": document.status,
                "list_type_label": _document_list_type_label(document),
                "row_count": row_count,
                "page_count": analysis_meta.get("total_pages", 0),
            }
        )
    return options


def build_row_view(doc_customer: DocumentCustomer, row: dict, confidence: dict | None = None) -> dict:
    confidence = confidence or {}
    status_key = _row_status_key(row)
    status_label, status_variant = STATUS_PRESENTATION[status_key]
    is_uncertain = _row_is_uncertain(confidence)
    start_date = row.get("contract_start_date")
    broker_number = (row.get("broker_number") or "").strip() if isinstance(row.get("broker_number"), str) else None
    customer = doc_customer.customer
    raw_customer = row.get("customer") or {}
    customer_date = customer.date_of_birth if customer is not None else raw_customer.get("date_of_birth")
    customer_postal = customer.postal_code if customer is not None else raw_customer.get("postal_code")
    customer_city = customer.city if customer is not None else raw_customer.get("city")

    return {
        "document_id": doc_customer.document_id,
        "document_name": doc_customer.document.original_filename,
        "document_uploaded_at": doc_customer.document.uploaded_at,
        "customer_id": doc_customer.customer_id,
        "customer_name": customer.name if customer else raw_customer.get("name", "Unbekannter Kunde"),
        "customer_date_of_birth": customer_date,
        "customer_date_of_birth_label": _format_date(customer_date),
        "customer_city": customer_city or "-",
        "customer_postal_code": customer_postal or "-",
        "contract_number": row.get("contract_number") or "-",
        "status_key": status_key,
        "status_code": (row.get("status_code") or "").strip().upper() or None,
        "status_label": status_label,
        "status_variant": status_variant,
        "product_line": row.get("product_line") or "-",
        "start_date_label": _format_date(start_date),
        "has_start_date": bool(start_date),
        "broker_number": broker_number or "-",
        "result_label": _row_result_label(row, status_key),
        "completion_label": "Abgeschlossen" if start_date else _row_completion_label(row, status_key),
        "completion_variant": "success" if start_date else ("danger" if row.get("is_storno") else "muted"),
        "safety_label": "Unklar" if is_uncertain else "Sicher",
        "safety_variant": "warning" if is_uncertain else "success",
        "reason": explain_category(row, classify_row(row)),
        "is_uncertain": is_uncertain,
        "source_page": row.get("source_page"),
        "source_page_label": f"Seite {row.get('source_page')}" if row.get("source_page") else "-",
        "source_row": row.get("source_row"),
        "raw_row": row,
        "confidence": confidence,
    }


def build_document_analysis(
    *,
    document_id: int | None,
    status_filter: str = "alle",
    current_broker_number: str | None = None,
    search_query: str | None = None,
    product_line_filter: str | None = None,
    group_by_customer: bool = True,
) -> dict:
    options = get_leipziger_document_options()
    selected_document_id = document_id or (options[0]["id"] if options else None)
    if selected_document_id is None:
        return {
            "document_options": options,
            "selected_document": None,
            "summary": _empty_summary(),
            "rows": [],
            "grouped_customers": [],
            "status_filter": status_filter,
            "status_filters": STATUS_FILTER_OPTIONS,
            "search_query": search_query or "",
            "product_line_filter": product_line_filter or "",
            "product_line_options": [],
            "group_by_customer": group_by_customer,
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
            "grouped_customers": [],
            "status_filter": status_filter,
            "status_filters": STATUS_FILTER_OPTIONS,
            "search_query": search_query or "",
            "product_line_filter": product_line_filter or "",
            "product_line_options": [],
            "group_by_customer": group_by_customer,
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
            item["source_page"] or 0,
            item["source_row"] or 0,
            item["customer_name"].lower(),
        )
    )

    filtered_rows = [
        row
        for row in rows
        if _matches_status_filter(row, status_filter)
        and _matches_product_line(row, product_line_filter)
        and _matches_search(row, search_query)
    ]

    grouped_customers = _build_grouped_customers(filtered_rows) if group_by_customer else []
    all_customer_groups = _build_grouped_customers(rows)
    summary = _build_summary(rows, all_customer_groups, reliable_ohne_antrag)
    analysis_meta = _analysis_meta(document)

    selected_document = {
        "id": document.id,
        "name": document.original_filename,
        "list_type_label": _document_list_type_label(document),
        "uploaded_at_label": document.uploaded_at.strftime("%d.%m.%Y %H:%M") if document.uploaded_at else "-",
        "vm_number_label": _document_broker_label(rows, current_broker_number),
        "page_count": analysis_meta.get("total_pages", 0),
        "processed_pages": analysis_meta.get("processed_pages", 0),
        "failed_page_count": analysis_meta.get("failed_page_count", 0),
        "failed_pages": analysis_meta.get("failed_pages", []),
        "row_count": analysis_meta.get("stored_row_count", len(rows)),
        "raw_row_count": analysis_meta.get("raw_row_count", len(rows)),
        "discarded_duplicate_count": analysis_meta.get("discarded_duplicate_count", 0),
        "uncertain_row_count": analysis_meta.get("uncertain_row_count", 0),
        "customer_count": analysis_meta.get("customer_count", len(all_customer_groups)),
        "visible_customer_count": len(grouped_customers) if group_by_customer else len(
            {row["customer_id"] or row["customer_name"] for row in filtered_rows}
        ),
        "status": document.status,
        "completion_label": str(analysis_meta.get("completion_label", "Analyse abgeschlossen")).replace("â€“", "-").replace("–", "-"),
        "is_complete": analysis_meta.get("is_complete", document.status.value == "done"),
        "show_ohne_antrag": reliable_ohne_antrag > 0,
        "raw_json": document.raw_json,
    }

    return {
        "document_options": options,
        "selected_document": selected_document,
        "summary": summary,
        "rows": filtered_rows,
        "grouped_customers": grouped_customers,
        "status_filter": status_filter,
        "status_filters": STATUS_FILTER_OPTIONS,
        "search_query": search_query or "",
        "product_line_filter": product_line_filter or "",
        "product_line_options": _product_line_options(rows),
        "group_by_customer": group_by_customer,
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
        confidence_rows = doc_customer.field_confidence or []
        for index, row in enumerate(doc_customer.row_data or []):
            row_view = build_row_view(doc_customer, row, confidence_rows[index] if index < len(confidence_rows) else {})
            row_category = classify_row(row)

            if category is not None and row_category != category:
                continue
            if not include_closed and category is None and row_view["has_start_date"]:
                continue
            if product_line and row_view["product_line"] != product_line:
                continue
            if broker_number and row_view["broker_number"] != broker_number:
                continue

            records.append(
                {
                    "customer_id": doc_customer.customer_id,
                    "customer_name": row_view["customer_name"],
                    "product": ", ".join(row.get("products") or []),
                    "product_line": row_view["product_line"],
                    "broker_number": row_view["broker_number"],
                    "category": row_category,
                    "angebotsdatum": document.uploaded_at,
                    "reason": row_view["reason"],
                    "document_id": document.id,
                }
            )
    return records


def get_analysis_summary(document: Document | None = None) -> dict:
    counters = _empty_summary()
    document_id = document.id if document is not None else None
    rows = []
    reliable_ohne_antrag = 0
    for doc_customer in _base_query(document_id, None, None, None).all():
        confidence_rows = doc_customer.field_confidence or []
        for index, row in enumerate(doc_customer.row_data or []):
            confidence = confidence_rows[index] if index < len(confidence_rows) else {}
            rows.append(build_row_view(doc_customer, row, confidence))
            if _is_reliably_without_antrag(row, confidence):
                reliable_ohne_antrag += 1
    grouped_customers = _build_grouped_customers(rows)
    return _build_summary(rows, grouped_customers, reliable_ohne_antrag)


def _build_grouped_customers(rows: list[dict]) -> list[dict]:
    groups: dict[tuple, dict] = {}
    duplicate_name_groups: dict[str, list[tuple]] = {}

    for row in rows:
        key = _group_key(row)
        group = groups.get(key)
        if group is None:
            group = {
                "group_key": key,
                "customer_id": row["customer_id"],
                "customer_name": row["customer_name"],
                "customer_date_of_birth_label": row["customer_date_of_birth_label"],
                "customer_city": row["customer_city"],
                "customer_postal_code": row["customer_postal_code"],
                "rows": [],
                "offer_count": 0,
                "closure_count": 0,
                "open_count": 0,
                "storno_count": 0,
                "possible_duplicate": False,
            }
            groups[key] = group
            duplicate_name_groups.setdefault(normalize_customer_name(row["customer_name"]), []).append(key)

        group["rows"].append(row)
        if row["status_key"] == "angebot":
            group["offer_count"] += 1
        if row["has_start_date"]:
            group["closure_count"] += 1
        if not row["has_start_date"] and row["status_key"] != "storno":
            group["open_count"] += 1
        if row["status_key"] == "storno":
            group["storno_count"] += 1

    for keys in duplicate_name_groups.values():
        if len(keys) < 2:
            continue
        for key in keys:
            groups[key]["possible_duplicate"] = True

    customer_groups = []
    for group in groups.values():
        rows_sorted = sorted(group["rows"], key=lambda item: (item["source_page"] or 0, item["source_row"] or 0))
        group["rows"] = rows_sorted
        group["record_count"] = len(rows_sorted)
        group["overall_status_label"], group["overall_status_variant"] = _group_status(group)
        customer_groups.append(group)

    customer_groups.sort(key=lambda item: item["customer_name"].lower())
    return customer_groups


def _build_summary(rows: list[dict], grouped_customers: list[dict], reliable_ohne_antrag: int) -> dict:
    return {
        "customers": len(grouped_customers),
        "contract_rows": len(rows),
        "total_records": len(rows),
        "angebote": sum(1 for row in rows if row["status_key"] == "angebot"),
        "neugeschaeft": sum(1 for row in rows if row["status_key"] == "neugeschaeft"),
        "fahrzeugwechsel": sum(1 for row in rows if row["status_key"] == "fahrzeugwechsel"),
        "abgeschlossen": sum(1 for row in rows if row["has_start_date"]),
        "offene_vorgaenge": sum(1 for row in rows if not row["has_start_date"] and row["status_key"] != "storno"),
        "ohne_beginn": sum(1 for row in rows if not row["has_start_date"]),
        "stornos": sum(1 for row in rows if row["status_key"] == "storno"),
        "unklar": sum(1 for row in rows if row["is_uncertain"] or row["status_key"] == "unklar"),
        "ohne_antrag": reliable_ohne_antrag,
    }


def _analysis_meta(document: Document) -> dict:
    return ((document.extra_data or {}).get("leipziger_analysis") or {})


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
    status_code = str(row.get("status_code") or "").strip().lower()
    if row.get("is_storno") or status_code == "storno":
        return "storno"
    if status_code == "fzw" or row.get("is_fahrzeugwechsel"):
        return "fahrzeugwechsel"
    if status_code == "neu" or row.get("is_neugeschaeft"):
        return "neugeschaeft"
    if status_code == "ang" or row.get("is_angebot"):
        return "angebot"
    return "unklar"


def _row_result_label(row: dict, status_key: str) -> str:
    if row.get("contract_start_date"):
        return "Beginn vorhanden"
    if status_key == "angebot":
        return "Angebot offen"
    if status_key == "neugeschaeft":
        return "Neugeschaeft ohne Beginn"
    if status_key == "fahrzeugwechsel":
        return "Fahrzeugwechsel offen"
    if status_key == "storno":
        return "Storno erkannt"
    return "Manuelle Pruefung"


def _row_completion_label(row: dict, status_key: str) -> str:
    if row.get("contract_start_date"):
        return "Abgeschlossen"
    if status_key == "storno":
        return "Storno"
    return "Offen"


def _group_key(row: dict) -> tuple:
    normalized_name = normalize_customer_name(row["customer_name"])
    if row["customer_date_of_birth"] not in (None, "-", ""):
        return ("dob", normalized_name, str(row["customer_date_of_birth"]))
    postal_code = row["customer_postal_code"]
    normalized_postal = normalize_postal_code(postal_code) if postal_code not in (None, "-", "") else ""
    if normalized_postal:
        return ("postal", normalized_name, normalized_postal)
    return ("customer", row["customer_id"] or row["customer_name"], row["customer_name"])


def _group_status(group: dict) -> tuple[str, str]:
    rows = group["rows"]
    if any(row["is_uncertain"] or row["status_key"] == "unklar" for row in rows):
        return "Unklar", "warning"
    if group["open_count"] == 0 and group["closure_count"] == group["record_count"]:
        return "Abgeschlossen", "success"
    if group["offer_count"] > 0:
        return "Angebote", "warning"
    if any(row["status_key"] == "fahrzeugwechsel" for row in rows):
        return "Fahrzeugwechsel", "info"
    if any(row["status_key"] == "neugeschaeft" for row in rows):
        return "Neugeschaeft", "info"
    if group["storno_count"] == group["record_count"]:
        return "Storno", "danger"
    return "Offen", "muted"


def _matches_status_filter(row_view: dict, status_filter: str) -> bool:
    if status_filter == "alle":
        return True
    if status_filter == "angebote":
        return row_view["status_key"] == "angebot"
    if status_filter == "neugeschaeft":
        return row_view["status_key"] == "neugeschaeft"
    if status_filter == "fahrzeugwechsel":
        return row_view["status_key"] == "fahrzeugwechsel"
    if status_filter == "abgeschlossen":
        return row_view["has_start_date"]
    if status_filter == "ohne_beginn":
        return not row_view["has_start_date"]
    if status_filter == "storno":
        return row_view["status_key"] == "storno"
    if status_filter == "unklar":
        return row_view["is_uncertain"] or row_view["status_key"] == "unklar"
    return True


def _matches_product_line(row_view: dict, product_line_filter: str | None) -> bool:
    if not product_line_filter:
        return True
    return row_view["product_line"].lower() == product_line_filter.lower()


def _matches_search(row_view: dict, search_query: str | None) -> bool:
    if not search_query:
        return True
    needle = search_query.strip().lower()
    return needle in row_view["customer_name"].lower() or needle in row_view["contract_number"].lower()


def _product_line_options(rows: list[dict]) -> list[str]:
    return sorted({row["product_line"] for row in rows if row["product_line"] and row["product_line"] != "-"})


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
        "customers": 0,
        "contract_rows": 0,
        "total_records": 0,
        "angebote": 0,
        "neugeschaeft": 0,
        "fahrzeugwechsel": 0,
        "abgeschlossen": 0,
        "offene_vorgaenge": 0,
        "ohne_beginn": 0,
        "stornos": 0,
        "unklar": 0,
        "ohne_antrag": 0,
    }
