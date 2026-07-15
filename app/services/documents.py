from app.extensions import db
from app.models import Customer, DocStatus, Document, DocumentCustomer
from app.models.enums import DocType, TimelineEventType
from app.services.analysis.validation import score_extraction, score_leipziger_liste_row
from app.services.llm.classification import compute_document_flags
from app.services.llm.recommendations import create_recommendations
from app.services.llm.schemas import DocumentExtraction, ExtractedCustomer, LeipzigerListeExtraction
from app.services.tasks import create_flag_based_tasks, create_tasks_from_recommendations
from app.services.timeline import log_timeline_event
from app.tenancy import get_current_tenant_id


def create_document(
    original_filename: str,
    stored_filename: str,
    file_path: str,
    tenant_id: int,
    uploaded_by_user_id: int | None = None,
) -> Document:
    document = Document(
        filename=stored_filename,
        original_filename=original_filename,
        file_path=file_path,
        status=DocStatus.PENDING,
        tenant_id=tenant_id,
        uploaded_by_user_id=uploaded_by_user_id,
    )
    db.session.add(document)
    db.session.commit()
    return document


def find_or_create_customer(data: ExtractedCustomer, uploaded_by_user_id: int | None = None) -> Customer:
    customer = Customer.query.filter_by(name=data.name).first()
    if customer is None:
        # M11: Bestandszuordnung wird nur beim erstmaligen Anlegen gesetzt und danach nie
        # ueberschrieben - "Mein Bestand" soll nicht durch spaetere Uploads eines anderen
        # Vermittlers fuer denselben Kunden umverteilt werden.
        customer = Customer(name=data.name, tenant_id=get_current_tenant_id(), assigned_user_id=uploaded_by_user_id)
        db.session.add(customer)

    customer.address = data.address or customer.address
    customer.city = data.city or customer.city
    customer.postal_code = data.postal_code or customer.postal_code
    customer.date_of_birth = data.date_of_birth or customer.date_of_birth
    return customer


def apply_extraction(document: Document, extraction: DocumentExtraction) -> None:
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
            extraction.customer, uploaded_by_user_id=document.uploaded_by_user_id
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


def apply_leipziger_liste_extraction(document: Document, extraction: LeipzigerListeExtraction) -> None:
    document.doc_type = DocType.LEIPZIGER_LISTE
    document.raw_json = extraction.model_dump(mode="json")

    for key, value in compute_document_flags(extraction).items():
        setattr(document, key, value)

    document_customers_by_id: dict[int, DocumentCustomer] = {}
    for index, row in enumerate(extraction.rows):
        row_customer = find_or_create_customer(row.customer, uploaded_by_user_id=document.uploaded_by_user_id)
        db.session.flush()  # Kunden-ID fuer den Abgleich mehrfacher Zeilen bereitstellen

        row_dict = row.model_dump(mode="json")
        row_confidence = score_leipziger_liste_row(row, document.raw_text or "")
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
