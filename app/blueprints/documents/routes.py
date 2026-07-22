from flask import Blueprint, abort, jsonify, redirect, render_template, request, send_file, url_for
from flask_login import current_user, login_required
from sqlalchemy.orm import joinedload

from app.extensions import db
from app.models import AnalysisRun, DocStatus, Document, ListComparison
from app.models.enums import DocType
from app.services.analysis.leipziger_liste_view import build_document_analysis
from app.services.document_progress import (
    build_document_progress,
    is_document_active_status,
    make_progress_snapshot,
    merge_progress_into_extra_data,
)
from app.services.storage import resolve_document_path
from app.tasks.document_tasks import process_document
from app.tenancy import get_or_404_scoped

documents_bp = Blueprint("documents", __name__, url_prefix="/documents")


def _document_list_query():
    return Document.query.options(joinedload(Document.customer)).order_by(Document.uploaded_at.desc())


def _build_document_summary(documents: list[Document]) -> dict[str, int]:
    summary = {"total": len(documents), "active": 0, "done": 0, "failed": 0}
    for document in documents:
        status_value = document.status.value if hasattr(document.status, "value") else str(document.status)
        if is_document_active_status(status_value):
            summary["active"] += 1
        elif status_value == DocStatus.DONE.value:
            summary["done"] += 1
        elif status_value == DocStatus.FAILED.value:
            summary["failed"] += 1
    return summary


def _build_detail_context(document: Document) -> tuple[ListComparison | None, dict | None]:
    list_comparison = ListComparison.query.filter_by(document_id=document.id).first()
    document_analysis = None
    if document.doc_type == DocType.LEIPZIGER_LISTE:
        document_analysis = build_document_analysis(
            document_id=document.id,
            current_broker_number=getattr(current_user, "vermittlernummer", None),
            group_by_customer=True,
        )
    return list_comparison, document_analysis


@documents_bp.route("")
@login_required
def list_documents():
    documents = _document_list_query().all()
    return render_template("documents/list.html", documents=documents, summary=_build_document_summary(documents))


@documents_bp.route("/<int:document_id>")
@login_required
def detail(document_id):
    document = get_or_404_scoped(Document, document_id)
    list_comparison, document_analysis = _build_detail_context(document)
    return render_template(
        "documents/detail.html",
        document=document,
        list_comparison=list_comparison,
        document_analysis=document_analysis,
    )


@documents_bp.route("/<int:document_id>/row")
@login_required
def row(document_id):
    document = get_or_404_scoped(Document, document_id)
    return render_template("documents/_row.html", document=document)


@documents_bp.route("/live")
@login_required
def live_documents():
    raw_ids = request.args.get("ids", "")
    document_ids = [int(value) for value in raw_ids.split(",") if value.strip().isdigit()]
    documents = _document_list_query().all()
    documents_by_id = {document.id: document for document in documents}
    target_ids = document_ids or [document.id for document in documents if is_document_active_status(document.status)]
    rows = []
    for document_id in target_ids:
        document = documents_by_id.get(document_id)
        if document is None:
            continue
        rows.append(
            {
                "id": document.id,
                "status": document.status.value,
                "active": is_document_active_status(document.status),
                "html": render_template("documents/_row.html", document=document),
            }
        )

    return jsonify(
        {
            "active_ids": [document.id for document in documents if is_document_active_status(document.status)],
            "summary_html": render_template("documents/_summary_cards.html", summary=_build_document_summary(documents)),
            "rows": rows,
        }
    )


@documents_bp.route("/<int:document_id>/live")
@login_required
def live_detail(document_id):
    document = get_or_404_scoped(Document, document_id)
    list_comparison, document_analysis = _build_detail_context(document)
    comparison_html = ""
    if list_comparison:
        comparison_html = render_template("documents/_list_comparison.html", list_comparison=list_comparison)
    return jsonify(
        {
            "document_id": document.id,
            "status": document.status.value,
            "active": is_document_active_status(document.status),
            "analysis_html": render_template(
                "documents/_analysis_panel.html",
                document=document,
                document_analysis=document_analysis,
            ),
            "history_html": render_template("documents/_verlauf.html", document=document),
            "comparison_html": comparison_html,
            "progress": build_document_progress(document),
        }
    )


@documents_bp.route("/<int:document_id>/file")
@login_required
def file(document_id):
    document = get_or_404_scoped(Document, document_id)
    path = resolve_document_path(document.file_path)
    if not path.exists():
        abort(404)
    return send_file(path, mimetype="application/pdf")


@documents_bp.route("/<int:document_id>/analysis-runs")
@login_required
def analysis_runs(document_id):
    document = get_or_404_scoped(Document, document_id)
    runs = AnalysisRun.query.filter_by(document_id=document.id).order_by(AnalysisRun.started_at.desc()).all()
    return jsonify(
        [
            {
                "id": run.id,
                "status": run.status.value,
                "engine_version": run.engine_version,
                "prompt_version": run.prompt_version,
                "openai_model": run.openai_model,
                "started_at": run.started_at.isoformat() if run.started_at else None,
                "finished_at": run.finished_at.isoformat() if run.finished_at else None,
                "duration_ms": run.duration_ms,
                "stage_durations": run.stage_durations,
                "overall_confidence": run.overall_confidence,
                "error_message": run.error_message,
                "summary": run.summary,
            }
            for run in runs
        ]
    )


@documents_bp.route("/<int:document_id>/retry", methods=["POST"])
@login_required
def retry(document_id):
    document = get_or_404_scoped(Document, document_id)
    document.status = DocStatus.PENDING
    document.error_message = None
    document.retry_count += 1
    document.extra_data = merge_progress_into_extra_data(
        document.extra_data,
        make_progress_snapshot(
            completed=["uploaded"],
            active="ocr",
            percent=12,
            headline="Analyse erneut eingeplant",
            detail="Der Worker startet den naechsten Verarbeitungsversuch.",
        ),
    )
    db.session.commit()
    process_document.delay(document.id)
    return redirect(url_for("documents.detail", document_id=document.id))
