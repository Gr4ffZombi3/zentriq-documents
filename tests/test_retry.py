from datetime import date

from app.models import Customer, DocStatus, Document, DocumentCustomer
from app.models.enums import DocType, OcrEngine
from app.services.llm.schemas import DocumentExtraction, ExtractedCustomer, LeipzigerListeExtraction, LeipzigerListeRow


def test_retry_reenqueues_failed_document(auth_client, db, tenant):
    document = Document(
        filename="failed.pdf",
        original_filename="failed.pdf",
        file_path="/does/not/exist.pdf",
        status=DocStatus.FAILED,
        error_message="OCR fehlgeschlagen: irgendwas",
        tenant_id=tenant.id,
    )
    db.session.add(document)
    db.session.commit()

    resp = auth_client.post(f"/documents/{document.id}/retry")
    assert resp.status_code == 302

    db.session.refresh(document)
    # Celery laeuft eager -> die (fehlschlagende OCR wegen fehlender Datei) ist bereits durch.
    assert document.retry_count == 1
    assert document.status == DocStatus.FAILED
    assert "OCR fehlgeschlagen" in document.error_message


def test_retry_replaces_old_leipziger_rows_without_duplicates(auth_client, db, tenant, monkeypatch):
    document = Document(
        filename="kw29_heller.pdf",
        original_filename="kw29_heller.pdf",
        file_path="/tmp/kw29_heller.pdf",
        status=DocStatus.FAILED,
        error_message="Teilweise ausgewertet - 1 von 4 Seiten verarbeitet",
        tenant_id=tenant.id,
        doc_type=DocType.LEIPZIGER_LISTE,
    )
    customer = Customer(tenant_id=tenant.id, name="Alt Kunde")
    db.session.add_all([document, customer])
    db.session.commit()
    db.session.add(
        DocumentCustomer(
            document=document,
            customer=customer,
            tenant_id=tenant.id,
            row_data=[{"contract_number": "OLD-1", "is_angebot": True}],
            field_confidence=[{}],
        )
    )
    db.session.commit()

    monkeypatch.setattr(
        "app.tasks.document_tasks.extract_text",
        lambda file_path: (
            "Seite 1\nSeite 2\nSeite 3\nSeite 4",
            OcrEngine.TESSERACT,
            96.0,
            ["Seite 1", "Seite 2", "Seite 3", "Seite 4"],
        ),
    )
    monkeypatch.setattr(
        "app.tasks.document_tasks.extract_document_data",
        lambda raw_text: DocumentExtraction(doc_type=DocType.LEIPZIGER_LISTE),
    )
    monkeypatch.setattr(
        "app.tasks.document_tasks.extract_leipziger_liste_rows",
        lambda page_texts: LeipzigerListeExtraction(
            rows=[
                LeipzigerListeRow(
                    customer=ExtractedCustomer(name="Neu Kunde"),
                    contract_number="A-100",
                    status_code="ANG",
                    is_angebot=True,
                    source_page=1,
                    source_row=1,
                ),
                LeipzigerListeRow(
                    customer=ExtractedCustomer(name="Neu Kunde"),
                    contract_number="B-200",
                    status_code="NEU",
                    is_neugeschaeft=True,
                    contract_start_date=date(2026, 7, 13),
                    source_page=2,
                    source_row=2,
                ),
            ],
            analysis_meta={
                "total_pages": 4,
                "processed_pages": 4,
                "processed_page_numbers": [1, 2, 3, 4],
                "failed_pages": [],
                "raw_row_count": 2,
                "batch_size": 1,
            },
        ),
    )

    resp = auth_client.post(f"/documents/{document.id}/retry")
    assert resp.status_code == 302

    db.session.refresh(document)
    assert document.retry_count == 1
    assert document.status == DocStatus.DONE
    assert document.error_message is None
    assert len(document.document_customers) == 2
    assert {
        row["contract_number"]
        for doc_customer in document.document_customers
        for row in (doc_customer.row_data or [])
    } == {"A-100", "B-200"}
