from celery import shared_task

from app.extensions import db
from app.models import DocStatus, Document
from app.services.ocr.pipeline import extract_text


@shared_task(bind=True)
def process_document(self, document_id: int):
    document = db.session.get(Document, document_id)
    if document is None:
        return

    document.status = DocStatus.OCR_PROCESSING
    db.session.commit()

    try:
        raw_text, engine_used, confidence = extract_text(document.file_path)
    except Exception as exc:
        document.status = DocStatus.FAILED
        document.error_message = f"OCR fehlgeschlagen: {exc}"
        db.session.commit()
        return

    document.raw_text = raw_text
    document.ocr_engine_used = engine_used
    document.ocr_confidence = confidence
    document.status = DocStatus.OCR_DONE
    db.session.commit()
