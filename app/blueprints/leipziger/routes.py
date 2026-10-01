from flask import Blueprint, abort, render_template, request
from flask_login import current_user, login_required

from app.auth.permissions import admin_required
from app.services import leipziger_todo
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
