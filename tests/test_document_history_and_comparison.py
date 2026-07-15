from datetime import datetime, timedelta, timezone

import fitz

from app.models import DocStatus, Document
from app.models.enums import DocType
from app.services.llm.schemas import (
    DocumentExtraction,
    ExtractedCustomer,
    LeipzigerListeExtraction,
    LeipzigerListeRow,
)
from app.tasks.document_tasks import process_document


def make_pdf_file(path):
    doc = fitz.open()
    doc.new_page()
    doc.save(str(path))
    doc.close()


def test_document_detail_shows_verlauf_without_regression_for_generic_doc(auth_client, db, tenant, user):
    document = Document(
        filename="rechnung.pdf",
        original_filename="rechnung.pdf",
        file_path="/tmp/rechnung.pdf",
        tenant_id=tenant.id,
        status=DocStatus.DONE,
        uploaded_by_user_id=user.id,
        ocr_confidence=93.5,
    )
    db.session.add(document)
    db.session.commit()

    resp = auth_client.get(f"/documents/{document.id}")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "Dokumentenverlauf" in body
    assert user.email in body
    assert "93.5" in body
    # Kein Leipziger-Liste-Vergleich fuer generische Dokumente ohne ListComparison.
    assert "Vergleich zur vorherigen Liste" not in body


def test_document_detail_shows_list_comparison_for_second_leipziger_liste_upload(
    app, db, tenant, tmp_path, monkeypatch, auth_client, user
):
    base_time = datetime.now(timezone.utc) - timedelta(days=1)

    def upload(filename, rows, uploaded_at):
        pdf_path = tmp_path / filename
        make_pdf_file(pdf_path)
        extraction = LeipzigerListeExtraction(rows=rows)
        monkeypatch.setattr(
            "app.tasks.document_tasks.extract_document_data",
            lambda raw_text: DocumentExtraction(doc_type=DocType.LEIPZIGER_LISTE),
        )
        monkeypatch.setattr(
            "app.tasks.document_tasks.extract_leipziger_liste_rows", lambda raw_text: extraction
        )
        with app.app_context():
            document = Document(
                filename=filename,
                original_filename=filename,
                file_path=str(pdf_path),
                status=DocStatus.PENDING,
                tenant_id=tenant.id,
                uploaded_by_user_id=user.id,
                uploaded_at=uploaded_at,
            )
            db.session.add(document)
            db.session.commit()
            process_document(document.id)
            db.session.refresh(document)
            return document

    upload(
        "v1.pdf",
        [LeipzigerListeRow(customer=ExtractedCustomer(name="Vergleich Kunde"))],
        base_time,
    )
    document2 = upload(
        "v2.pdf",
        [
            LeipzigerListeRow(customer=ExtractedCustomer(name="Vergleich Kunde")),
            LeipzigerListeRow(customer=ExtractedCustomer(name="Neuer Kunde")),
        ],
        base_time + timedelta(days=7),
    )

    resp = auth_client.get(f"/documents/{document2.id}")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "Vergleich zur vorherigen Liste" in body
    assert "v1.pdf" in body
    assert "Neuer Kunde" in body
