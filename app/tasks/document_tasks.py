from datetime import datetime, timezone

from celery import shared_task

from app.extensions import db
from app.models import DocStatus, Document
from app.models.enums import DocType
from app.services.documents import apply_extraction, apply_leipziger_liste_extraction
from app.services.list_comparison import compare_leipziger_liste
from app.services.llm.extraction import extract_document_data, extract_leipziger_liste_rows
from app.services.ocr.pipeline import extract_text
from app.tenancy import bypass_tenant_scope, use_tenant_id


@shared_task(bind=True)
def process_document(self, document_id: int):
    # Celery-Worker laufen ausserhalb eines Request-Kontexts, daher gibt es noch keinen
    # Tenant-Kontext. Der initiale Lookup per ID ist bewusst ungescoped (vertrauenswuerdiger
    # Backend-Code, die ID stammt von bereits autorisiertem Code) - direkt danach wird der
    # Tenant-Kontext auf den des geladenen Dokuments gesetzt, sodass alle nachgelagerten
    # Queries (Customer-Upsert etc.) automatisch korrekt gescoped sind. use_tenant_id() stellt
    # danach den vorherigen Kontext wieder her, statt ihn hart zurueckzusetzen - im Eager-Modus
    # (Tests/Dev) laeuft der Task sonst im selben Kontext wie der aufrufende Request und wuerde
    # dessen Tenant-Kontext zerstoeren.
    with bypass_tenant_scope():
        document = db.session.get(Document, document_id)
    if document is None:
        return

    with use_tenant_id(document.tenant_id):
        _run_pipeline(document)


def _run_pipeline(document: Document) -> None:
    # Idempotent machen: bei einem (Retry-)Durchlauf duerfen keine Reste aus einer
    # vorherigen Verarbeitung (z.B. document_customers) uebrig bleiben, sonst verletzt ein
    # erneuter Insert den Unique-Constraint auf (tenant_id, document_id, customer_id).
    document.recommendations = []
    document.document_customers = []
    document.tasks = []
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
            db.session.flush()  # document_customers-IDs fuer den Listenvergleich bereitstellen
            compare_leipziger_liste(document)
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
