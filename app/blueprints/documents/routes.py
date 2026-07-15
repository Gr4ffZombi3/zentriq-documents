from flask import Blueprint, abort, redirect, render_template, send_file, url_for
from flask_login import login_required

from app.extensions import db
from app.models import DocStatus, Document, ListComparison
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
    return render_template("documents/detail.html", document=document, list_comparison=list_comparison)


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
