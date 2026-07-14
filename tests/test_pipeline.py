import fitz

from app.models import DocStatus, Document, OcrEngine
from app.services.ocr.pipeline import extract_text
from app.tasks.document_tasks import process_document


def make_pdf_file(path):
    doc = fitz.open()
    doc.new_page()
    doc.save(str(path))
    doc.close()


def test_extract_text_uses_tesseract_when_confidence_is_high(app, tmp_path):
    pdf_path = tmp_path / "sample.pdf"
    make_pdf_file(pdf_path)

    with app.app_context():
        text, engine, confidence = extract_text(str(pdf_path))

    assert text == "Erkannter Testtext aus Tesseract."
    assert engine == OcrEngine.TESSERACT
    assert confidence == 96.0


def test_extract_text_falls_back_to_vision_on_low_confidence(app, tmp_path, monkeypatch):
    pdf_path = tmp_path / "scan.pdf"
    make_pdf_file(pdf_path)

    monkeypatch.setattr(
        "app.services.ocr.tesseract_ocr.ocr_image", lambda image: ("", 10.0)
    )
    monkeypatch.setattr(
        "app.services.ocr.vision_ocr.ocr_image", lambda image: "Vision-erkannter Text."
    )

    with app.app_context():
        text, engine, confidence = extract_text(str(pdf_path))

    assert text == "Vision-erkannter Text."
    assert engine == OcrEngine.VISION
    assert confidence is None


def test_process_document_task_runs_ocr_and_ai_extraction(app, db, tenant, tmp_path):
    pdf_path = tmp_path / "task_test.pdf"
    make_pdf_file(pdf_path)

    with app.app_context():
        document = Document(
            filename="task_test.pdf",
            original_filename="task_test.pdf",
            file_path=str(pdf_path),
            status=DocStatus.PENDING,
            tenant_id=tenant.id,
        )
        db.session.add(document)
        db.session.commit()

        process_document(document.id)

        db.session.refresh(document)
        assert document.status == DocStatus.DONE
        assert document.raw_text == "Erkannter Testtext aus Tesseract."
        assert document.ocr_engine_used == OcrEngine.TESSERACT
        assert document.customer.name == "Max Mustermann"
        assert document.raw_json is not None


def test_process_document_task_marks_failed_on_error(app, db, tenant, tmp_path):
    with app.app_context():
        document = Document(
            filename="missing.pdf",
            original_filename="missing.pdf",
            file_path=str(tmp_path / "does_not_exist.pdf"),
            status=DocStatus.PENDING,
            tenant_id=tenant.id,
        )
        db.session.add(document)
        db.session.commit()

        process_document(document.id)

        db.session.refresh(document)
        assert document.status == DocStatus.FAILED
        assert document.error_message is not None
