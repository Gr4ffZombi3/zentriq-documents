from datetime import datetime, timezone

from celery import shared_task

from app.extensions import db
from app.models import DocStatus, Document
from app.models.enums import DocType
from app.services.documents import apply_extraction, apply_leipziger_liste_extraction
from app.services.llm.extraction import extract_document_data, extract_leipziger_liste_rows
from app.services.ocr.pipeline import extract_text


@shared_task(bind=True)
def process_document(self, document_id: int):
    document = db.session.get(Document, document_id)
    if document is None:
        return

    # Idempotent machen: bei einem (Retry-)Durchlauf duerfen keine Reste aus einer
    # vorherigen Verarbeitung (z.B. document_customers) uebrig bleiben, sonst verletzt ein
    # erneuter Insert den Unique-Constraint auf (document_id, customer_id).
    document.recommendations = []
    document.document_customers = []
    document.status = DocStatus.OCR_PROCESSING
    db.session.commit()

    try:
        raw_text, engine_used, confidence = extract_text(document.file_path)
    except Exception as exc:
        db.session.rollback()
        document.status = DocStatus.FAILED
        document.error_message = f"OCR fehlgeschlagen: {exc}"
        db.session.commit()
        return

    document.raw_text = raw_text
    document.ocr_engine_used = engine_used
    document.ocr_confidence = confidence
    document.status = DocStatus.AI_PROCESSING
    db.session.commit()

    try:
        extraction = extract_document_data(raw_text)
        if extraction.doc_type == DocType.LEIPZIGER_LISTE:
            leipziger_extraction = extract_leipziger_liste_rows(raw_text)
            apply_leipziger_liste_extraction(document, leipziger_extraction)
        else:
            apply_extraction(document, extraction)
    except Exception as exc:
        db.session.rollback()
        document.status = DocStatus.FAILED
        document.error_message = f"KI-Analyse fehlgeschlagen: {exc}"
        db.session.commit()
        return

    document.status = DocStatus.DONE
    document.processed_at = datetime.now(timezone.utc)
    db.session.commit()
