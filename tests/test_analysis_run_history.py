import fitz

from app.models import AnalysisRun, AnalysisRunStatus, DocStatus, Document
from app.tasks.document_tasks import process_document


def make_pdf_file(path):
    doc = fitz.open()
    doc.new_page()
    doc.save(str(path))
    doc.close()


def test_reprocessing_grows_analysis_run_history_while_document_stays_idempotent(app, db, tenant, tmp_path):
    pdf_path = tmp_path / "history.pdf"
    make_pdf_file(pdf_path)

    with app.app_context():
        document = Document(
            filename="history.pdf",
            original_filename="history.pdf",
            file_path=str(pdf_path),
            status=DocStatus.PENDING,
            tenant_id=tenant.id,
        )
        db.session.add(document)
        db.session.commit()

        process_document(document.id)
        db.session.refresh(document)

        assert document.status == DocStatus.DONE
        assert AnalysisRun.query.filter_by(document_id=document.id).count() == 1

        first_run = AnalysisRun.query.filter_by(document_id=document.id).one()
        assert first_run.status == AnalysisRunStatus.SUCCEEDED
        assert first_run.duration_ms is not None
        assert first_run.duration_ms >= 0
        assert first_run.stage_durations["ocr"] is not None
        assert first_run.stage_durations["layout"] is not None
        assert first_run.stage_durations["tables"] is not None
        assert first_run.stage_durations["extraction_and_rules"] is not None
        assert first_run.overall_confidence is not None
        assert first_run.engine_version
        assert first_run.prompt_version
        assert first_run.openai_model

        first_insurer = document.insurer
        first_contract_number = document.contract_number

        # Zweiter Durchlauf (wie ein Retry): AnalysisRun-Historie waechst, Document-Spalten
        # bleiben wie bisher idempotent (gleiche gemockte Extraktion -> gleiche Werte).
        process_document(document.id)
        db.session.refresh(document)

        assert document.status == DocStatus.DONE
        assert document.insurer == first_insurer
        assert document.contract_number == first_contract_number

        runs = AnalysisRun.query.filter_by(document_id=document.id).order_by(AnalysisRun.started_at).all()
        assert len(runs) == 2
        assert all(run.status == AnalysisRunStatus.SUCCEEDED for run in runs)


def test_failed_ocr_still_creates_a_failed_analysis_run(app, db, tenant, tmp_path):
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

        run = AnalysisRun.query.filter_by(document_id=document.id).one()
        assert run.status == AnalysisRunStatus.FAILED
        assert run.error_message is not None
        assert "OCR fehlgeschlagen" in run.error_message
        assert run.overall_confidence is None
