from flask import Blueprint, current_app, make_response, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from app.models.enums import ListScope, ListType
from app.services.documents import create_document
from app.services.storage import save_pdf
from app.tasks.document_tasks import process_document
from app.utils.validation import InvalidPDFError, validate_pdf

upload_bp = Blueprint("upload", __name__, url_prefix="/upload")


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
    if request.headers.get("HX-Request"):
        response = make_response("")
        response.headers["HX-Redirect"] = detail_url
        return response

    return redirect(detail_url)
