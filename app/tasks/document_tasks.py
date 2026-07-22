import time
from datetime import datetime, timezone

from celery import shared_task
from flask import current_app

from app.extensions import db
from app.models import AnalysisRun, DocStatus, Document
from app.models.enums import AnalysisRunStatus, ComparisonKind, DocType
from app.services.analysis.layout import detect_layout
from app.services.analysis.list_scope_detection import detect_list_scope
from app.services.analysis.report import build_analysis_report
from app.services.analysis.tables import detect_tables
from app.services.documents import apply_extraction, apply_leipziger_liste_extraction
from app.services.list_comparison import compare_leipziger_liste, find_paired_gs_or_own_document
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
    document.customer = None
    document.raw_json = None
    document.extra_data = None
    document.processed_at = None
    document.status = DocStatus.OCR_PROCESSING
    db.session.commit()
    current_app.logger.info(
        "document.analysis.started tenant_id=%s document_id=%s status=%s",
        document.tenant_id,
        document.id,
        document.status.value,
    )

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
        current_app.logger.exception(
            "document.analysis.ocr_failed tenant_id=%s document_id=%s",
            document.tenant_id,
            document.id,
        )
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
    current_app.logger.info(
        "document.analysis.ocr_done tenant_id=%s document_id=%s engine=%s confidence=%s",
        document.tenant_id,
        document.id,
        engine_used.value if hasattr(engine_used, "value") else engine_used,
        confidence,
    )

    stage_start = time.monotonic()
    layout_info = detect_layout(raw_text, page_texts)
    stage_durations["layout"] = round((time.monotonic() - stage_start) * 1000, 1)

    stage_start = time.monotonic()
    table_info = detect_tables(raw_text)
    stage_durations["tables"] = round((time.monotonic() - stage_start) * 1000, 1)

    document.status = DocStatus.AI_PROCESSING
    db.session.commit()
    current_app.logger.info(
        "document.analysis.ai_started tenant_id=%s document_id=%s status=%s",
        document.tenant_id,
        document.id,
        document.status.value,
    )

    stage_start = time.monotonic()
    try:
        extraction = extract_document_data(raw_text)
        if extraction.doc_type == DocType.LEIPZIGER_LISTE:
            leipziger_extraction = extract_leipziger_liste_rows(page_texts)
            apply_stats = apply_leipziger_liste_extraction(document, leipziger_extraction)
            analysis_meta = _build_leipziger_analysis_meta(page_texts, leipziger_extraction, apply_stats)
            document.extra_data = {
                **(document.extra_data or {}),
                "leipziger_analysis": analysis_meta,
            }
            db.session.flush()  # document_customers-IDs fuer den Listenvergleich bereitstellen
            if analysis_meta["is_complete"]:
                compare_leipziger_liste(document)
                # M13: nur automatisch erkennen, wenn beim Upload keine manuelle Auswahl getroffen wurde.
                if document.list_scope is None:
                    document.list_scope = detect_list_scope(document)
                # M13: zusaetzlicher Vergleich Eigene Liste <-> GS-Liste, neben dem obigen
                # immer laufenden zeitbasierten Vergleich - nur wenn ein Gegenstueck existiert.
                paired_document = find_paired_gs_or_own_document(document)
                if paired_document is not None:
                    compare_leipziger_liste(
                        document, previous_document=paired_document, comparison_kind=ComparisonKind.OWN_VS_GS
                    )
        else:
            apply_extraction(document, extraction)
    except Exception as exc:
        stage_durations["extraction_and_rules"] = round((time.monotonic() - stage_start) * 1000, 1)
        db.session.rollback()
        document.status = DocStatus.FAILED
        document.error_message = f"KI-Analyse fehlgeschlagen: {exc}"
        db.session.commit()
        current_app.logger.exception(
            "document.analysis.ai_failed tenant_id=%s document_id=%s",
            document.tenant_id,
            document.id,
        )
        _finish_run(AnalysisRunStatus.FAILED, error_message=document.error_message)
        return
    stage_durations["extraction_and_rules"] = round((time.monotonic() - stage_start) * 1000, 1)

    leipziger_meta = (document.extra_data or {}).get("leipziger_analysis")
    if leipziger_meta and not leipziger_meta["is_complete"]:
        document.status = DocStatus.FAILED
        document.error_message = str(leipziger_meta["completion_label"]).replace("â€“", "-").replace("–", "-")
    else:
        document.status = DocStatus.DONE
        document.error_message = None
    document.processed_at = datetime.now(timezone.utc)
    db.session.commit()
    current_app.logger.info(
        "document.analysis.completed tenant_id=%s document_id=%s status=%s duration_ms=%s",
        document.tenant_id,
        document.id,
        document.status.value,
        int((time.monotonic() - pipeline_start) * 1000),
    )

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
            "leipziger_analysis": (document.extra_data or {}).get("leipziger_analysis"),
            **build_analysis_report(document),
        },
        overall_confidence=_compute_overall_confidence(document),
    )


def _build_leipziger_analysis_meta(page_texts: list[str], extraction, apply_stats: dict) -> dict:
    analysis_meta = getattr(extraction, "analysis_meta", {}) or {}
    total_pages = len(page_texts)
    processed_pages = int(analysis_meta.get("processed_pages", total_pages))
    failed_pages = list(analysis_meta.get("failed_pages", []))
    processed_page_numbers = list(analysis_meta.get("processed_page_numbers", list(range(1, total_pages + 1))))
    stored_rows = int(apply_stats.get("stored_rows", len(getattr(extraction, "rows", []))))
    discarded_duplicates = int(apply_stats.get("discarded_duplicates", 0))
    uncertain_rows = int(apply_stats.get("uncertain_rows", 0))
    raw_row_count = int(analysis_meta.get("raw_row_count", len(getattr(extraction, "rows", []))))
    is_complete = processed_pages == total_pages and not failed_pages

    if is_complete:
        completion_label = f"Vollstaendig ausgewertet – {processed_pages} von {total_pages} Seiten verarbeitet"
    else:
        completion_label = f"Teilweise ausgewertet – {processed_pages} von {total_pages} Seiten verarbeitet"

    return {
        "total_pages": total_pages,
        "processed_pages": processed_pages,
        "processed_page_numbers": processed_page_numbers,
        "failed_pages": failed_pages,
        "failed_page_count": len(failed_pages),
        "raw_row_count": raw_row_count,
        "stored_row_count": stored_rows,
        "discarded_duplicate_count": discarded_duplicates,
        "uncertain_row_count": uncertain_rows,
        "is_complete": is_complete,
        "completion_label": completion_label,
        "page_batch_size": int(analysis_meta.get("batch_size", 1)),
    }
