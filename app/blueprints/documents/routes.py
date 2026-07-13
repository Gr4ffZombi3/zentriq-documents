from flask import Blueprint, abort, render_template, send_file

from app.extensions import db
from app.models import Document
from app.services.storage import resolve_document_path

documents_bp = Blueprint("documents", __name__, url_prefix="/documents")


@documents_bp.route("")
def list_documents():
    documents = Document.query.order_by(Document.uploaded_at.desc()).all()
    return render_template("documents/list.html", documents=documents)


@documents_bp.route("/<int:document_id>")
def detail(document_id):
    document = db.get_or_404(Document, document_id)
    return render_template("documents/detail.html", document=document)


@documents_bp.route("/<int:document_id>/file")
def file(document_id):
    document = db.get_or_404(Document, document_id)
    path = resolve_document_path(document.file_path)
    if not path.exists():
        abort(404)
    return send_file(path, mimetype="application/pdf")
