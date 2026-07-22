from flask import Blueprint, abort, jsonify, redirect, render_template, send_file, url_for
from flask_login import current_user, login_required

from app.extensions import db
from app.models import AnalysisRun, DocStatus, Document, ListComparison
from app.models.enums import DocType
from app.services.analysis.leipziger_liste_view import build_document_analysis
from app.services.storage import resolve_document_path
from app.tasks.document_tasks import process_document
from app.tenancy import get_or_404_scoped

documents_bp = Blueprint("documents", __name__, url_prefix="/documents")


@documents_bp.route("")
@login_required
def list_documents():
    documents = Document.query.order_by(Document.uploaded_at.desc()).all()
    return render_template("documents/list.html", documents=documents)


@documents_bp.route("/<int:document_id>")
@login_required
def detail(document_id):
    document = get_or_404_scoped(Document, document_id)
    list_comparison = ListComparison.query.filter_by(document_id=document.id).first()
    document_analysis = None
    if document.doc_type == DocType.LEIPZIGER_LISTE:
        document_analysis = build_document_analysis(
            document_id=document.id,
            current_broker_number=getattr(current_user, "vermittlernummer", None),
            group_by_customer=True,
        )
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
    db.session.commit()
    process_document.delay(document.id)
    return redirect(url_for("documents.detail", document_id=document.id))
