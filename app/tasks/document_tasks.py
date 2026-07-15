import time
from datetime import datetime, timezone

from celery import shared_task
from flask import current_app

from app.extensions import db
from app.models import AnalysisRun, DocStatus, Document
from app.models.enums import AnalysisRunStatus, DocType
from app.services.analysis.layout import detect_layout
from app.services.analysis.tables import detect_tables
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


def _compute_overall_confidence(document: Document) -> float | None:
    scores: list[float] = []
    if document.field_confidence:
        scores.extend(entry["confidence"] for entry in document.field_confidence.values())
    for doc_customer in document.document_customers:
        for row_confidence in doc_customer.field_confidence or []:
            scores.extend(entry["confidence"] for entry in row_confidence.values())
    if not scores:
        return None
    return round(sum(scores) / len(scores), 1)


def _run_pipeline(document: Document) -> None:
    # Idempotent machen: bei einem (Retry-)Durchlauf duerfen keine Reste aus einer
    # vorherigen Verarbeitung (z.B. document_customers) uebrig bleiben, sonst verletzt ein
    # erneuter Insert den Unique-Constraint auf (tenant_id, document_id, customer_id).
    # AnalysisRun wird bewusst NICHT zurueckgesetzt - jeder Versuch bleibt als eigene Zeile
    # in der Analyse-Historie erhalten (M12).
    document.recommendations = []
    document.document_customers = []
    document.tasks = []
    document.status = DocStatus.OCR_PROCESSING
    db.session.commit()

    run = AnalysisRun(
        tenant_id=document.tenant_id,
        document_id=document.id,
        engine_version=current_app.config["ANALYSIS_ENGINE_VERSION"],
        prompt_version=current_app.config["ANALYSIS_PROMPT_VERSION"],
        openai_model=current_app.config["OPENAI_MODEL"],
        status=AnalysisRunStatus.RUNNING,
    )
    db.session.add(run)
    db.session.commit()

    stage_durations: dict[str, float] = {}
    pipeline_start = time.monotonic()

    def _finish_run(
        status: AnalysisRunStatus,
        error_message: str | None = None,
        summary: dict | None = None,
        overall_confidence: float | None = None,
    ) -> None:
        run.status = status
        run.finished_at = datetime.now(timezone.utc)
        run.duration_ms = int((time.monotonic() - pipeline_start) * 1000)
        run.stage_durations = dict(stage_durations)
        run.error_message = error_message
        if summary is not None:
            run.summary = summary
        if overall_confidence is not None:
            run.overall_confidence = overall_confidence
        db.session.commit()

    stage_start = time.monotonic()
    try:
        raw_text, engine_used, confidence, page_texts = extract_text(document.file_path)
    except Exception as exc:
        db.session.rollback()
        document.status = DocStatus.FAILED
        document.error_message = f"OCR fehlgeschlagen: {exc}"
        db.session.commit()
        _finish_run(AnalysisRunStatus.FAILED, error_message=document.error_message)
        return
    stage_durations["ocr"] = round((time.monotonic() - stage_start) * 1000, 1)

    document.raw_text = raw_text
    document.ocr_engine_used = engine_used
    document.ocr_confidence = confidence
    # M12: OCR_DONE wird jetzt tatsaechlich erreicht (war zuvor nur im Enum definiert, nie
    # gesetzt) - die UI hat dafuer bereits ein fertiges Badge, keine Template-Aenderung noetig.
    document.status = DocStatus.OCR_DONE
    db.session.commit()

    stage_start = time.monotonic()
    layout_info = detect_layout(raw_text, page_texts)
    stage_durations["layout"] = round((time.monotonic() - stage_start) * 1000, 1)

    stage_start = time.monotonic()
    table_info = detect_tables(raw_text)
    stage_durations["tables"] = round((time.monotonic() - stage_start) * 1000, 1)

    document.status = DocStatus.AI_PROCESSING
    db.session.commit()

    stage_start = time.monotonic()
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
        stage_durations["extraction_and_rules"] = round((time.monotonic() - stage_start) * 1000, 1)
        db.session.rollback()
        document.status = DocStatus.FAILED
        document.error_message = f"KI-Analyse fehlgeschlagen: {exc}"
        db.session.commit()
        _finish_run(AnalysisRunStatus.FAILED, error_message=document.error_message)
        return
    stage_durations["extraction_and_rules"] = round((time.monotonic() - stage_start) * 1000, 1)

    document.status = DocStatus.DONE
    document.processed_at = datetime.now(timezone.utc)
    db.session.commit()

    _finish_run(
        AnalysisRunStatus.SUCCEEDED,
        summary={
            "layout": {
                "page_count_estimate": layout_info.page_count_estimate,
                "has_footnotes": layout_info.has_footnotes,
                "footnote_count": len(layout_info.footnote_lines),
            },
            "tables": {
                "table_row_count": table_info.table_row_count,
                "table_block_count": table_info.table_block_count,
            },
        },
        overall_confidence=_compute_overall_confidence(document),
    )
