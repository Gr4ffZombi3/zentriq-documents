from datetime import date

from app.extensions import db
from app.models import DocStatus, Document, DocumentCustomer
from app.models.enums import DocType, ListScope, ListType, TimelineEventType
from app.services.analysis.business_rules import (
    count_offer_occurrences,
    create_advanced_recommendations,
    customer_has_ever_closed,
)
from app.services.analysis.validation import score_extraction, score_leipziger_liste_row
from app.services.customers import CustomerMatcher, normalize_customer_name
from app.services.llm.classification import compute_document_flags
from app.services.llm.recommendations import create_recommendations
from app.services.llm.schemas import DocumentExtraction, ExtractedCustomer, LeipzigerListeExtraction
from app.services.tasks import create_flag_based_tasks, create_tasks_from_recommendations
from app.services.timeline import log_timeline_event


def create_document(
    original_filename: str,
    stored_filename: str,
    file_path: str,
    tenant_id: int,
    uploaded_by_user_id: int | None = None,
    list_scope: ListScope | None = None,
    list_type: ListType | None = None,
    extra_data: dict | None = None,
) -> Document:
    document = Document(
        filename=stored_filename,
        original_filename=original_filename,
        file_path=file_path,
        status=DocStatus.PENDING,
        tenant_id=tenant_id,
        uploaded_by_user_id=uploaded_by_user_id,
        # M13: manuell gewaehlter Listentyp, falls beim Upload angegeben - die automatische
        # Erkennung in der Pipeline greift dann nicht mehr (siehe document_tasks.py).
        list_scope=list_scope,
        list_type=list_type,
        extra_data=extra_data,
    )
    db.session.add(document)
    db.session.commit()
    return document


def find_or_create_customer(
    data: ExtractedCustomer,
    uploaded_by_user_id: int | None = None,
    matcher: CustomerMatcher | None = None,
):
    active_matcher = matcher or CustomerMatcher()
    return active_matcher.get_or_create(data, uploaded_by_user_id=uploaded_by_user_id)


def apply_extraction(document: Document, extraction: DocumentExtraction) -> None:
    matcher = CustomerMatcher()
    document.doc_type = extraction.doc_type
    document.vehicle = extraction.vehicle
    document.license_plate = extraction.license_plate
    document.insurer = extraction.insurer
    document.contract_number = extraction.contract_number
    document.case_number = extraction.case_number
    document.broker = extraction.broker
    document.contract_start_date = extraction.contract_start_date
    document.products = extraction.products
    document.special_notes = extraction.special_notes
    # M12: nur bei generischen Einzeldokumenten auf Dokumentebene gesetzt - bei Leipziger-Liste-
    # Dokumenten (mehrere Kunden pro PDF) waere ein Dokument-weiter Wert hier irrefuehrend, siehe
    # apply_leipziger_liste_extraction() (bleibt dort bewusst NULL, Daten stecken in row_data).
    document.broker_number = extraction.broker_number
    document.product_line = extraction.product_line
    document.premium = extraction.premium
    document.tariff = extraction.tariff
    document.raw_json = extraction.model_dump(mode="json")
    document.field_confidence = score_extraction(extraction, document.raw_text or "")

    if extraction.customer is not None:
        document.customer = find_or_create_customer(
            extraction.customer,
            uploaded_by_user_id=document.uploaded_by_user_id,
            matcher=matcher,
        )
        log_timeline_event(
            document.customer,
            TimelineEventType.DOCUMENT_UPLOADED,
            f"Dokument hochgeladen: {document.original_filename}",
            document=document,
            occurred_at=document.uploaded_at,
        )

    recommendations = create_recommendations(
        document,
        document.customer,
        products=extraction.products,
        vehicle=extraction.vehicle,
    )
    create_tasks_from_recommendations(document, document.customer, recommendations)


def apply_leipziger_liste_extraction(document: Document, extraction: LeipzigerListeExtraction) -> dict:
    matcher = CustomerMatcher()
    document.doc_type = DocType.LEIPZIGER_LISTE
    document.raw_json = extraction.model_dump(mode="json")

    prepared_rows, duplicate_count = _deduplicate_leipziger_rows(extraction.rows)
    prepared_extraction = LeipzigerListeExtraction(rows=prepared_rows, analysis_meta=extraction.analysis_meta)

    for key, value in compute_document_flags(prepared_extraction).items():
        setattr(document, key, value)

    document_customers_by_id: dict[int, DocumentCustomer] = {}
    stored_rows = 0
    uncertain_rows = 0
    for index, row in enumerate(prepared_rows):
        row_customer = find_or_create_customer(
            row.customer,
            uploaded_by_user_id=document.uploaded_by_user_id,
            matcher=matcher,
        )
        db.session.flush()  # Kunden-ID fuer den Abgleich mehrfacher Zeilen bereitstellen

        row_dict = row.model_dump(mode="json")
        row_confidence = score_leipziger_liste_row(row, document.raw_text or "")
        stored_rows += 1
        if any(entry.get("uncertain") for entry in row_confidence.values()):
            uncertain_rows += 1
        existing = document_customers_by_id.get(row_customer.id)
        if existing is None:
            doc_customer = DocumentCustomer(
                document=document,
                customer=row_customer,
                row_data=[row_dict],
                field_confidence=[row_confidence],
                tenant_id=document.tenant_id,
            )
            db.session.add(doc_customer)
            document_customers_by_id[row_customer.id] = doc_customer
            log_timeline_event(
                row_customer,
                TimelineEventType.DOCUMENT_UPLOADED,
                f"Dokument hochgeladen: {document.original_filename}",
                document=document,
                occurred_at=document.uploaded_at,
            )
        else:
            existing.row_data = [*(existing.row_data or []), row_dict]
            existing.field_confidence = [*(existing.field_confidence or []), row_confidence]

        for flag_value, flag_event_type, flag_label in (
            (row.is_angebot, TimelineEventType.OFFER_DETECTED, "Angebot erkannt"),
            (row.is_neugeschaeft, TimelineEventType.NEW_CONTRACT_DETECTED, "Neuer Vertrag erkannt"),
            (row.is_fahrzeugwechsel, TimelineEventType.VEHICLE_CHANGE_DETECTED, "Fahrzeugwechsel erkannt"),
            (row.is_storno, TimelineEventType.STORNO_DETECTED, "Storno erkannt"),
        ):
            if flag_value:
                log_timeline_event(
                    row_customer, flag_event_type, flag_label, document=document, occurred_at=document.uploaded_at
                )

        if index == 0:
            document.customer = row_customer

        row_recommendations = create_recommendations(
            document,
            row_customer,
            products=row.products,
            vehicle=row.vehicle,
            is_neugeschaeft=row.is_neugeschaeft,
            is_fahrzeugwechsel=row.is_fahrzeugwechsel,
            cross_sell_opportunity=row.cross_sell_opportunity,
            priority=row.priority,
        )
        create_tasks_from_recommendations(document, row_customer, row_recommendations)
        create_flag_based_tasks(document, row_customer, row)

        # M12: erweiterte Business-Regeln (Cross-Selling nach Sparten-Luecke, Vertriebsrisiko,
        # Storno-Prioritaet) - eigenstaendige Regelschicht, beruehrt build_recommendations() nicht.
        advanced_recommendations = create_advanced_recommendations(
            document,
            row_customer,
            products=row.products,
            vehicle=row.vehicle,
            is_storno=row.is_storno,
            sibling_offer_count=count_offer_occurrences(row_customer.id),
            has_closed=customer_has_ever_closed(row_customer.id),
        )
        create_tasks_from_recommendations(document, row_customer, advanced_recommendations)

    return {
        "stored_rows": stored_rows,
        "discarded_duplicates": duplicate_count,
        "uncertain_rows": uncertain_rows,
        "customer_count": len(document_customers_by_id),
    }


def _deduplicate_leipziger_rows(rows) -> tuple[list, int]:
    deduplicated = []
    by_key = {}
    duplicate_count = 0

    for row in rows:
        key = _leipziger_row_key(row)
        existing = by_key.get(key)
        if existing is None:
            by_key[key] = row
            deduplicated.append(row)
            continue
        duplicate_count += 1
        _merge_leipziger_rows(existing, row)

    return deduplicated, duplicate_count


def _leipziger_row_key(row) -> tuple:
    contract_number = _normalized_text(row.contract_number)
    status_code = _row_status_code(row)
    product_line = _normalized_text(row.product_line)
    start_value = row.contract_start_date.isoformat() if isinstance(row.contract_start_date, date) else str(row.contract_start_date or "")
    source_page = row.source_page or 0
    source_row = row.source_row or 0

    if contract_number:
        return ("contract", contract_number, status_code, product_line, start_value)

    products = tuple(sorted(_normalized_text(product) for product in row.products if product))
    customer_key = normalize_customer_name(row.customer.name)
    return (
        "position",
        customer_key,
        status_code,
        product_line,
        start_value,
        products,
        source_page,
        source_row,
    )


def _merge_leipziger_rows(existing, incoming) -> None:
    for field_name in (
        "vehicle",
        "license_plate",
        "insurer",
        "contract_number",
        "recommended_next_action",
        "special_notes",
        "broker_number",
        "product_line",
        "premium",
        "tariff",
        "contract_start_date",
        "status_code",
    ):
        if getattr(existing, field_name) in (None, "", []) and getattr(incoming, field_name) not in (None, "", []):
            setattr(existing, field_name, getattr(incoming, field_name))

    existing.products = sorted({*(existing.products or []), *(incoming.products or [])})
    existing.is_neugeschaeft = existing.is_neugeschaeft or incoming.is_neugeschaeft
    existing.is_fahrzeugwechsel = existing.is_fahrzeugwechsel or incoming.is_fahrzeugwechsel
    existing.is_angebot = existing.is_angebot or incoming.is_angebot
    existing.is_storno = existing.is_storno or incoming.is_storno
    existing.cross_sell_opportunity = existing.cross_sell_opportunity or incoming.cross_sell_opportunity
    existing.has_multiple_products = existing.has_multiple_products or incoming.has_multiple_products
    existing.has_antrag = existing.has_antrag or incoming.has_antrag
    source_pages = [value for value in (existing.source_page, incoming.source_page) if value is not None]
    if source_pages:
        existing.source_page = min(source_pages)
    source_rows = [value for value in (existing.source_row, incoming.source_row) if value is not None]
    if source_rows:
        existing.source_row = min(source_rows)


def _row_status_code(row) -> str:
    if row.status_code:
        return _normalized_text(row.status_code)
    if row.is_storno:
        return "storno"
    if row.is_fahrzeugwechsel:
        return "fzw"
    if row.is_neugeschaeft:
        return "neu"
    if row.is_angebot:
        return "ang"
    return ""


def _normalized_text(value) -> str:
    return str(value or "").strip().lower()
