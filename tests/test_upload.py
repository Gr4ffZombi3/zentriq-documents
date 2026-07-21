import io

import fitz

from app.models import Document, ListScope, ListType


def make_pdf_bytes() -> bytes:
    doc = fitz.open()
    doc.new_page()
    data = doc.tobytes()
    doc.close()
    return data


def test_upload_valid_pdf_creates_document_and_redirects(auth_client, db, user):
    pdf_bytes = make_pdf_bytes()
    resp = auth_client.post(
        "/upload",
        data={"file": (io.BytesIO(pdf_bytes), "Rechnung.pdf"), "list_type": "other"},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 302

    document = Document.query.first()
    assert document is not None
    assert document.original_filename == "Rechnung.pdf"
    # Celery läuft im Test-Modus synchron (CELERY_TASK_ALWAYS_EAGER), daher sind die
    # (gemockte) OCR- und KI-Verarbeitung zu diesem Zeitpunkt bereits abgeschlossen.
    assert document.status.value == "done"
    assert document.raw_text == "Erkannter Testtext aus Tesseract."
    assert document.customer.name == "Max Mustermann"
    assert document.insurer == "Testversicherung AG"
    assert document.list_type == ListType.OTHER
    # M11: Uploader und Bestandszuordnung werden durchgereicht.
    assert document.uploaded_by_user_id == user.id
    assert document.customer.assigned_user_id == user.id


def test_upload_with_manual_list_type_selection_sets_type_and_scope(auth_client, db):
    pdf_bytes = make_pdf_bytes()
    resp = auth_client.post(
        "/upload",
        data={"file": (io.BytesIO(pdf_bytes), "Liste.pdf"), "list_type": "own"},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 302
    document = Document.query.first()
    assert document.list_type == ListType.OWN
    assert document.list_scope == ListScope.OWN


def test_upload_with_comparison_type_leaves_list_scope_automatic(auth_client, db):
    pdf_bytes = make_pdf_bytes()
    auth_client.post(
        "/upload",
        data={"file": (io.BytesIO(pdf_bytes), "Liste2.pdf"), "list_type": "comparison"},
        content_type="multipart/form-data",
    )
    document = Document.query.first()
    assert document.list_type == ListType.COMPARISON
    assert document.list_scope is None


def test_upload_requires_login(client):
    pdf_bytes = make_pdf_bytes()
    resp = client.post(
        "/upload",
        data={"file": (io.BytesIO(pdf_bytes), "Rechnung.pdf"), "list_type": "other"},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 302
    assert "/auth/login" in resp.headers["Location"]
    assert Document.query.count() == 0


def test_upload_rejects_non_pdf(auth_client, db):
    resp = auth_client.post(
        "/upload",
        data={"file": (io.BytesIO(b"not a pdf"), "note.txt"), "list_type": "other"},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 400
    assert Document.query.count() == 0


def test_document_list_and_detail_and_file_serving(auth_client, db):
    pdf_bytes = make_pdf_bytes()
    auth_client.post(
        "/upload",
        data={"file": (io.BytesIO(pdf_bytes), "Gutachten.pdf"), "list_type": "other"},
        content_type="multipart/form-data",
    )
    document = Document.query.first()

    list_resp = auth_client.get("/documents")
    assert list_resp.status_code == 200
    assert "Gutachten.pdf" in list_resp.get_data(as_text=True)

    detail_resp = auth_client.get(f"/documents/{document.id}")
    assert detail_resp.status_code == 200
    assert "Gutachten.pdf" in detail_resp.get_data(as_text=True)

    file_resp = auth_client.get(f"/documents/{document.id}/file")
    assert file_resp.status_code == 200
    assert file_resp.mimetype == "application/pdf"


def test_document_list_renders_upload_form_with_standard_post_fallback(auth_client):
    resp = auth_client.get("/documents")
    html = resp.get_data(as_text=True)

    assert resp.status_code == 200
    assert 'action="/upload"' in html
    assert 'method="post"' in html
    assert 'enctype="multipart/form-data"' in html
    assert 'name="csrf_token"' in html
    assert 'name="list_type"' in html
    assert "Welche Leipziger Liste wird hochgeladen?" in html
    assert "Dokumente einreichen" in html
