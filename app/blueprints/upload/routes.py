from flask import Blueprint, make_response, redirect, render_template, request, url_for

from app.services.documents import create_document
from app.services.storage import save_pdf
from app.utils.validation import InvalidPDFError, validate_pdf

upload_bp = Blueprint("upload", __name__, url_prefix="/upload")


@upload_bp.route("", methods=["POST"])
def upload_document():
    file = request.files.get("file")
    if file is None or file.filename == "":
        return render_template("components/upload_widget.html", error="Bitte eine PDF-Datei auswählen."), 400

    file_bytes = file.read()
    try:
        validate_pdf(file.filename, file_bytes)
    except InvalidPDFError as exc:
        return render_template("components/upload_widget.html", error=str(exc)), 400

    stored_filename, file_path = save_pdf(file_bytes)
    document = create_document(file.filename, stored_filename, file_path)

    detail_url = url_for("documents.detail", document_id=document.id)
    if request.headers.get("HX-Request"):
        response = make_response("")
        response.headers["HX-Redirect"] = detail_url
        return response

    return redirect(detail_url)
