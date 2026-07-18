import fitz

from app.models import DocStatus, Document, ListScope
from app.services.analysis.list_scope_detection import detect_list_scope
from app.services.llm.schemas import (
    DocumentExtraction,
    ExtractedCustomer,
    LeipzigerListeExtraction,
    LeipzigerListeRow,
)
from app.tasks.document_tasks import process_document


def make_document(db, tenant_id, filename="liste.pdf"):
    document = Document(
        filename=filename, original_filename=filename, file_path=f"/tmp/{filename}", tenant_id=tenant_id
    )
    db.session.add(document)
    db.session.commit()
    return document


def link_rows(db, tenant_id, document, customer_name, broker_number):
    from app.models import Customer, DocumentCustomer

    customer = Customer(tenant_id=tenant_id, name=customer_name)
    db.session.add(customer)
    db.session.commit()
    dc = DocumentCustomer(
        document=document, customer=customer, tenant_id=tenant_id, row_data=[{"broker_number": broker_number}]
    )
    db.session.add(dc)
    db.session.commit()


def test_no_broker_numbers_defaults_to_own(app, db, tenant):
    document = make_document(db, tenant.id)
    link_rows(db, tenant.id, document, "Kunde A", None)
    assert detect_list_scope(document) == ListScope.OWN


def test_single_broker_number_is_own(app, db, tenant):
    document = make_document(db, tenant.id)
    link_rows(db, tenant.id, document, "Kunde A", "VM-1001")
    link_rows(db, tenant.id, document, "Kunde B", "VM-1001")
    assert detect_list_scope(document) == ListScope.OWN


def test_multiple_broker_numbers_is_geschaeftsstelle(app, db, tenant):
    document = make_document(db, tenant.id)
    link_rows(db, tenant.id, document, "Kunde A", "VM-1001")
    link_rows(db, tenant.id, document, "Kunde B", "VM-2002")
    assert detect_list_scope(document) == ListScope.GESCHAEFTSSTELLE


def make_pdf_file(path):
    doc = fitz.open()
    doc.new_page()
    doc.save(str(path))
    doc.close()


def test_pipeline_sets_list_scope_automatically(app, db, tenant, tmp_path, monkeypatch):
    from app.models.enums import DocType

    pdf_path = tmp_path / "auto.pdf"
    make_pdf_file(pdf_path)

    extraction = LeipzigerListeExtraction(
        rows=[
            LeipzigerListeRow(customer=ExtractedCustomer(name="Kunde A"), broker_number="VM-1001"),
            LeipzigerListeRow(customer=ExtractedCustomer(name="Kunde B"), broker_number="VM-2002"),
        ]
    )
    monkeypatch.setattr(
        "app.tasks.document_tasks.extract_document_data",
        lambda raw_text: DocumentExtraction(doc_type=DocType.LEIPZIGER_LISTE),
    )
    monkeypatch.setattr("app.tasks.document_tasks.extract_leipziger_liste_rows", lambda raw_text: extraction)

    document = Document(
        filename="auto.pdf", original_filename="auto.pdf", file_path=str(pdf_path),
        status=DocStatus.PENDING, tenant_id=tenant.id,
    )
    db.session.add(document)
    db.session.commit()

    process_document(document.id)
    db.session.refresh(document)

    assert document.list_scope == ListScope.GESCHAEFTSSTELLE


def test_manual_list_scope_overrides_automatic_detection(app, db, tenant, tmp_path, monkeypatch):
    pdf_path = tmp_path / "manual.pdf"
    make_pdf_file(pdf_path)

    # Zeilen deuten auf GESCHAEFTSSTELLE hin (2 Vermittlernummern), aber der Nutzer hat beim
    # Upload manuell OWN gewaehlt - die Erkennung darf das nicht ueberschreiben.
    extraction = LeipzigerListeExtraction(
        rows=[
            LeipzigerListeRow(customer=ExtractedCustomer(name="Kunde A"), broker_number="VM-1001"),
            LeipzigerListeRow(customer=ExtractedCustomer(name="Kunde B"), broker_number="VM-2002"),
        ]
    )
    from app.models.enums import DocType

    monkeypatch.setattr(
        "app.tasks.document_tasks.extract_document_data",
        lambda raw_text: DocumentExtraction(doc_type=DocType.LEIPZIGER_LISTE),
    )
    monkeypatch.setattr("app.tasks.document_tasks.extract_leipziger_liste_rows", lambda raw_text: extraction)

    document = Document(
        filename="manual.pdf", original_filename="manual.pdf", file_path=str(pdf_path),
        status=DocStatus.PENDING, tenant_id=tenant.id, list_scope=ListScope.OWN,
    )
    db.session.add(document)
    db.session.commit()

    process_document(document.id)
    db.session.refresh(document)

    assert document.list_scope == ListScope.OWN
