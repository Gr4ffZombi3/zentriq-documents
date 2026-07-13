import io

import fitz

from app.models import Document


def make_pdf_bytes() -> bytes:
    doc = fitz.open()
    doc.new_page()
    data = doc.tobytes()
    doc.close()
    return data


def test_upload_valid_pdf_creates_document_and_redirects(client, db):
    pdf_bytes = make_pdf_bytes()
    resp = client.post(
        "/upload",
        data={"file": (io.BytesIO(pdf_bytes), "Rechnung.pdf")},
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


def test_upload_rejects_non_pdf(client, db):
    resp = client.post(
        "/upload",
        data={"file": (io.BytesIO(b"not a pdf"), "note.txt")},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 400
    assert Document.query.count() == 0


def test_document_list_and_detail_and_file_serving(client, db):
    pdf_bytes = make_pdf_bytes()
    client.post(
        "/upload",
        data={"file": (io.BytesIO(pdf_bytes), "Gutachten.pdf")},
        content_type="multipart/form-data",
    )
    document = Document.query.first()

    list_resp = client.get("/documents")
    assert list_resp.status_code == 200
    assert "Gutachten.pdf" in list_resp.get_data(as_text=True)

    detail_resp = client.get(f"/documents/{document.id}")
    assert detail_resp.status_code == 200
    assert "Gutachten.pdf" in detail_resp.get_data(as_text=True)

    file_resp = client.get(f"/documents/{document.id}/file")
    assert file_resp.status_code == 200
    assert file_resp.mimetype == "application/pdf"
