from app.extensions import db
from app.models import Customer, DocStatus, Document
from app.services.llm.schemas import DocumentExtraction, ExtractedCustomer


def create_document(original_filename: str, stored_filename: str, file_path: str) -> Document:
    document = Document(
        filename=stored_filename,
        original_filename=original_filename,
        file_path=file_path,
        status=DocStatus.PENDING,
    )
    db.session.add(document)
    db.session.commit()
    return document


def find_or_create_customer(data: ExtractedCustomer) -> Customer:
    customer = Customer.query.filter_by(name=data.name).first()
    if customer is None:
        customer = Customer(name=data.name)
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
    document.raw_json = extraction.model_dump(mode="json")

    if extraction.customer is not None:
        document.customer = find_or_create_customer(extraction.customer)
