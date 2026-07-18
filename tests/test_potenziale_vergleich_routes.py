from datetime import datetime, timedelta, timezone

import fitz

from app.models import DocStatus, Document, ListScope
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


def upload_leipziger_liste(app, db, tenant, tmp_path, monkeypatch, filename, rows, uploaded_at):
    pdf_path = tmp_path / filename
    make_pdf_file(pdf_path)

    extraction = LeipzigerListeExtraction(rows=rows)
    monkeypatch.setattr(
        "app.tasks.document_tasks.extract_document_data",
        lambda raw_text: DocumentExtraction(doc_type=DocType.LEIPZIGER_LISTE),
    )
    monkeypatch.setattr("app.tasks.document_tasks.extract_leipziger_liste_rows", lambda raw_text: extraction)

    with app.app_context():
        document = Document(
            filename=filename,
            original_filename=filename,
            file_path=str(pdf_path),
            status=DocStatus.PENDING,
            tenant_id=tenant.id,
            uploaded_at=uploaded_at,
        )
        db.session.add(document)
        db.session.commit()

        process_document(document.id)
        db.session.refresh(document)
        return document


def test_vergleich_requires_login(client):
    resp = client.get("/potenziale/vergleich")
    assert resp.status_code == 302
    assert "/auth/login" in resp.headers["Location"]


def test_vergleich_renders_empty_state_without_comparisons(auth_client, db):
    resp = auth_client.get("/potenziale/vergleich")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "Noch kein Vergleich vorhanden" in body


def test_vergleich_renders_synthetic_own_gs_pair(app, db, tenant, tmp_path, monkeypatch, auth_client):
    base_time = datetime.now(timezone.utc) - timedelta(days=2)

    own_document = upload_leipziger_liste(
        app, db, tenant, tmp_path, monkeypatch, "vergleich_own.pdf",
        rows=[
            LeipzigerListeRow(customer=ExtractedCustomer(name="Kunde A"), broker_number="VM-1001"),
            LeipzigerListeRow(customer=ExtractedCustomer(name="Kunde B"), broker_number="VM-1001"),
        ],
        uploaded_at=base_time,
    )
    gs_document = upload_leipziger_liste(
        app, db, tenant, tmp_path, monkeypatch, "vergleich_gs.pdf",
        rows=[
            LeipzigerListeRow(customer=ExtractedCustomer(name="Kunde A"), broker_number="VM-1001"),
            LeipzigerListeRow(customer=ExtractedCustomer(name="Kunde C"), broker_number="VM-2002"),
        ],
        uploaded_at=base_time + timedelta(days=1),
    )
    assert own_document.list_scope == ListScope.OWN
    assert gs_document.list_scope == ListScope.GESCHAEFTSSTELLE

    resp = auth_client.get("/potenziale/vergleich")
    body = resp.get_data(as_text=True)
    assert "vergleich_own.pdf" in body
    assert "vergleich_gs.pdf" in body
    assert "Kunde C" in body
    assert "Kunde B" in body


def test_vergleich_document_id_selects_specific_comparison(app, db, tenant, tmp_path, monkeypatch, auth_client):
    base_time = datetime.now(timezone.utc) - timedelta(days=2)

    upload_leipziger_liste(
        app, db, tenant, tmp_path, monkeypatch, "sel_own.pdf",
        rows=[LeipzigerListeRow(customer=ExtractedCustomer(name="Kunde A"), broker_number="VM-1001")],
        uploaded_at=base_time,
    )
    gs_document = upload_leipziger_liste(
        app, db, tenant, tmp_path, monkeypatch, "sel_gs.pdf",
        rows=[
            LeipzigerListeRow(customer=ExtractedCustomer(name="Kunde A"), broker_number="VM-1001"),
            LeipzigerListeRow(customer=ExtractedCustomer(name="Kunde D"), broker_number="VM-9999"),
        ],
        uploaded_at=base_time + timedelta(days=1),
    )

    resp = auth_client.get(f"/potenziale/vergleich?document_id={gs_document.id}")
    body = resp.get_data(as_text=True)
    assert "Kunde D" in body
