from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from app.auth.permissions import admin_required
from app.extensions import db
from app.models import DocStatus, Document
from app.models.audit_log import AuditEventType
from app.models.enums import DocType
from app.services import leipziger_todo
from app.services.audit import log_audit_event
from app.services.document_progress import make_progress_snapshot, merge_progress_into_extra_data
from app.services.storage import resolve_document_path
from app.tasks.document_tasks import process_document
from app.tenancy import get_or_404_scoped
from app.utils.vermittlernummer import vermittlernummer_key

leipziger_bp = Blueprint("leipziger", __name__, url_prefix="/leipziger-liste")


PAGE_SIZE = 100


@leipziger_bp.route("")
@login_required
def index():
    """Arbeitsliste mit Reitern "Zu erledigen", "Mit Datum", "Ohne Datum" und Suche nach
    Vertrags-/Vermittlernummer. Admins sehen alle Zeilen ihres Bueros, Mitarbeiter
    ausschliesslich die ueber ihre eigene Vermittlernummer zugeordneten (serverseitig)."""
    documents = leipziger_todo.list_documents()
    document = leipziger_todo.select_document(documents, request.args.get("document_id", type=int))
    own_key = vermittlernummer_key(current_user.vermittlernummer)
    entries = leipziger_todo.visible_entries(
        leipziger_todo.load_entries(document), own_key, all_brokers=current_user.is_admin
    )
    query = (request.args.get("q") or "").strip()[:60]
    entries = leipziger_todo.search_entries(entries, query)
    tab = request.args.get("tab")
    if tab not in leipziger_todo.TABS:
        tab = "zu-erledigen"
    items = leipziger_todo.tab_entries(entries, tab)

    total = len(items)
    pages = max(1, -(-total // PAGE_SIZE))
    page = min(max(request.args.get("seite", 1, type=int), 1), pages)
    return render_template(
        "leipziger/index.html",
        documents=documents,
        document=document,
        items=items[(page - 1) * PAGE_SIZE : page * PAGE_SIZE],
        total=total,
        page=page,
        pages=pages,
        tab=tab,
        tabs=leipziger_todo.TABS,
        counts=leipziger_todo.tab_counts(entries),
        query=query,
        broker_names=leipziger_todo.broker_names(leipziger_todo.team_members()) if current_user.is_admin else {},
        has_own_number=own_key is not None,
        import_stats=leipziger_todo.import_stats(document) if current_user.is_admin else None,
    )


@leipziger_bp.route("/mitarbeiter")
@login_required
@admin_required
def team():
    documents = leipziger_todo.list_documents()
    document = leipziger_todo.select_document(documents, request.args.get("document_id", type=int))
    entries = leipziger_todo.load_entries(document)
    members = []
    for user in leipziger_todo.team_members():
        key = vermittlernummer_key(user.vermittlernummer)
        members.append(
            {
                "user": user,
                "name": leipziger_todo.user_display_name(user),
                "open": leipziger_todo.open_entries(entries, key),
                "done": leipziger_todo.done_entries(entries, key),
            }
        )

    selected_id = request.args.get("mitarbeiter", type=int)
    selected = next((m for m in members if m["user"].id == selected_id), None)
    if selected_id is not None and selected is None:
        abort(404)
    if selected is None and members:
        selected = members[0]
    view = "mit" if request.args.get("ansicht") == "mit" else "ohne"
    return render_template(
        "leipziger/team.html",
        documents=documents,
        document=document,
        members=members,
        selected=selected,
        view=view,
    )


@leipziger_bp.post("/<int:document_id>/neu-einlesen")
@login_required
@admin_required
def reimport(document_id):
    """Verwirft die bisherige Auswertung dieser Liste (Vorgaenge, Aufgaben, Empfehlungen) und
    liest dieselbe PDF vollstaendig neu ein. Die PDF selbst bleibt unveraendert gespeichert."""
    document = get_or_404_scoped(Document, document_id)
    if document.doc_type != DocType.LEIPZIGER_LISTE:
        abort(404)
    try:
        if not resolve_document_path(document.file_path).is_file():
            raise FileNotFoundError(document.file_path)
    except (ValueError, FileNotFoundError):
        flash("Die PDF dieser Liste ist nicht mehr vorhanden. Bitte die Liste neu hochladen.", "error")
        return redirect(url_for("leipziger.index"))

    document.status = DocStatus.PENDING
    document.error_message = None
    document.retry_count += 1
    document.extra_data = merge_progress_into_extra_data(
        document.extra_data,
        make_progress_snapshot(
            completed=["uploaded"],
            active="ocr",
            percent=12,
            headline="Neu einlesen eingeplant",
            detail="Die Liste wird vollstaendig neu ausgewertet.",
        ),
    )
    db.session.commit()
    log_audit_event(
        AuditEventType.LEIPZIGER_LIST_UPLOADED,
        user=current_user,
        details={"document_id": document.id, "reimport": True},
    )
    process_document.delay(document.id)
    flash("Die Liste wird neu eingelesen. Das dauert in der Regel nur wenige Sekunden – bitte die Seite danach neu laden.", "success")
    return redirect(url_for("leipziger.index"))
