from flask import Blueprint, current_app, jsonify, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from app.models import Document
from app.models.enums import DocStatus, ListScope, ListType
from app.services.document_progress import make_progress_snapshot, merge_progress_into_extra_data
from app.services.documents import create_document
from app.services.storage import save_pdf
from app.tasks.document_tasks import process_document
from app.utils.validation import InvalidPDFError, validate_pdf

upload_bp = Blueprint("upload", __name__, url_prefix="/upload")


def _is_async_request() -> bool:
    return request.headers.get("X-Requested-With") == "XMLHttpRequest" or request.accept_mimetypes.best == "application/json"


def _build_summary() -> dict[str, int]:
    counts = {"total": 0, "active": 0, "done": 0, "failed": 0}
    status_rows = Document.query.with_entities(Document.status).all()
    counts["total"] = len(status_rows)
    for (status,) in status_rows:
        status_value = status.value if hasattr(status, "value") else str(status)
        if status_value in {
            DocStatus.PENDING.value,
            DocStatus.OCR_PROCESSING.value,
            DocStatus.OCR_DONE.value,
            DocStatus.AI_PROCESSING.value,
        }:
            counts["active"] += 1
        elif status_value == DocStatus.DONE.value:
            counts["done"] += 1
        elif status_value == DocStatus.FAILED.value:
            counts["failed"] += 1
    return counts


@upload_bp.route("", methods=["POST"])
@login_required
def upload_document():
    list_type_raw = request.form.get("list_type", "")
    try:
        list_type = ListType(list_type_raw) if list_type_raw else ListType.OTHER
    except ValueError:
        list_type = ListType.OTHER

    file = request.files.get("file")
    if file is None or file.filename == "":
        current_app.logger.warning(
            "document.upload.rejected tenant_id=%s user_id=%s reason=missing_file",
            current_user.tenant_id,
            current_user.id,
        )
        if _is_async_request():
            return jsonify({"error": "Bitte eine PDF-Datei auswaehlen."}), 400
        return render_template("components/upload_widget.html", error="Bitte eine PDF-Datei auswaehlen."), 400

    file_bytes = file.read()
    try:
        validate_pdf(file.filename, file_bytes)
    except InvalidPDFError as exc:
        current_app.logger.warning(
            "document.upload.rejected tenant_id=%s user_id=%s filename=%s reason=invalid_pdf detail=%s",
            current_user.tenant_id,
            current_user.id,
            file.filename,
            exc,
        )
        if _is_async_request():
            return jsonify({"error": str(exc)}), 400
        return render_template("components/upload_widget.html", error=str(exc)), 400

    list_scope = {
        ListType.OWN: ListScope.OWN,
        ListType.GS: ListScope.GESCHAEFTSSTELLE,
    }.get(list_type)

    current_app.logger.info(
        "document.upload.accepted tenant_id=%s user_id=%s filename=%s bytes=%s list_type=%s list_scope=%s",
        current_user.tenant_id,
        current_user.id,
        file.filename,
        len(file_bytes),
        list_type.value,
        list_scope.value if list_scope else "auto",
    )
    stored_filename, file_path = save_pdf(file_bytes)
    document = create_document(
        file.filename,
        stored_filename,
        file_path,
        tenant_id=current_user.tenant_id,
        uploaded_by_user_id=current_user.id,
        list_scope=list_scope,
        list_type=list_type,
        extra_data=merge_progress_into_extra_data(
            None,
            make_progress_snapshot(
                completed=["uploaded"],
                active="ocr",
                percent=12,
                headline="Upload abgeschlossen",
                detail="Das Dokument ist gespeichert und wartet auf den Analyse-Start.",
            ),
        ),
    )
    current_app.logger.info(
        "document.upload.stored tenant_id=%s user_id=%s document_id=%s path=%s",
        current_user.tenant_id,
        current_user.id,
        document.id,
        file_path,
    )
    process_document.delay(document.id)
    current_app.logger.info(
        "document.upload.analysis_enqueued tenant_id=%s user_id=%s document_id=%s",
        current_user.tenant_id,
        current_user.id,
        document.id,
    )

    detail_url = url_for("documents.detail", document_id=document.id)
    if _is_async_request():
        return (
            jsonify(
                {
                    "document_id": document.id,
                    "detail_url": detail_url,
                    "row_html": render_template("documents/_row.html", document=document),
                    "summary_html": render_template("documents/_summary_cards.html", summary=_build_summary()),
                    "status": document.status.value,
                }
            ),
            201,
        )

    return redirect(detail_url)
