from flask import Blueprint, abort, flash, jsonify, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from app.auth.permissions import admin_required
from app.models import Customer, DocumentCustomer, Task
from app.models.enums import TaskStatus
from app.navigation import display_name_for
from app.services import customer_create, customer_notes, customer_sources
from app.services.customer_duplicates import (
    REASON_LABELS,
    MergeError,
    compare_rows,
    link_counts,
    match_reason,
    merge_customers,
)
from app.services.customer_overview import customer_entries, customer_memos
from app.services.customers import (
    DEFAULT_CUSTOMER_PAGE_SIZE,
    MAX_CUSTOMER_PAGE_SIZE,
    build_customer_detail_context,
    build_customer_directory,
)
from app.services.huk import jobs as huk_jobs
from app.tenancy import get_or_404_scoped
from app.utils.vermittlernummer import vermittlernummer_key

MEMO_BASIS_LABELS = {
    "customer_number": "über die Kundennummer erkannt",
    "phone": "über die Telefonnummer erkannt",
    "selected": "manuell zugeordnet",
    "new_customer": "Kunde aus Memo angelegt",
}

customers_bp = Blueprint("customers", __name__, url_prefix="/customers")


@customers_bp.route("")
@login_required
def list_customers():
    page = request.args.get("page", 1, type=int)
    per_page = request.args.get("per_page", DEFAULT_CUSTOMER_PAGE_SIZE, type=int)
    directory = build_customer_directory(page=page, per_page=per_page, query=request.args.get("q", ""))
    return render_template(
        "customers/list.html",
        customer_directory=directory,
        customers=[item["customer"] for item in directory["items"]],
        max_per_page=MAX_CUSTOMER_PAGE_SIZE,
    )


# --- Kunde anlegen (Maske mit "Kundendaten einfuegen", app/services/customer_intake.py) -------------


@customers_bp.route("/neu", methods=["GET", "POST"])
@login_required
def new():
    """Kunde anlegen. Pflicht: Vor- und Nachname. Kein Dublettenverdacht -> ein Klick auf Speichern;
    sonst passende Akten mit "Akte öffnen" und nur mit "Trotzdem anlegen"."""
    from app.services import customer_intake
    from app.services.customer_create import CreateError

    if request.method == "GET":
        return render_template("customers/new.html", values={}, duplicates=[])
    form = {key: request.form.get(key, "") for key in customer_intake.FIELDS}
    try:
        values = customer_intake.clean_values(form)
        customer, found = customer_intake.create(current_user, values, allow_duplicate=request.form.get("trotzdem") == "1")
    except CreateError as exc:
        return render_template("customers/new.html", values=form, duplicates=[], error=str(exc)), exc.status if exc.status != 400 else 422
    if customer is None:
        return render_template(
            "customers/new.html", values=form, duplicates=[customer_intake.serialize_duplicate(c, r) for c, r in found],
            error="Es gibt bereits eine passende Akte. Bitte prüfen – oder „Trotzdem als neuen Kunden anlegen“ wählen.",
        ), 409
    flash(f"{customer.name} wurde angelegt.", "success")
    return redirect(url_for("customers.detail", customer_id=customer.id))


@customers_bp.post("/neu/erkennen")
@login_required
def new_detect():
    """Eingefuegten Text erkennen - speichert nichts."""
    from app.services import customer_intake

    data = request.get_json(silent=True) or {}
    text = data.get("text") if isinstance(data.get("text"), str) else ""
    return jsonify({"fields": customer_intake.parse_block(text)})


@customers_bp.post("/neu/pruefen")
@login_required
def new_check():
    """Dublettenverdacht zu den aktuellen Angaben (eigenes Buero) - speichert nichts."""
    from app.services import customer_intake
    from app.services.customer_create import CreateError

    data = request.get_json(silent=True) or {}
    form = {key: data.get(key) if isinstance(data.get(key), str) else "" for key in customer_intake.FIELDS}
    try:
        values = customer_intake.clean_values(form)
    except CreateError:
        values = {k: v for k, v in form.items() if v}
        if form.get("first_name") and form.get("last_name"):
            values["name"] = f"{form['first_name'].strip()} {form['last_name'].strip()}"
    found = customer_intake.duplicates(values) if values else []
    return jsonify({"duplicates": [customer_intake.serialize_duplicate(c, r) for c, r in found]})


@customers_bp.route("/<int:customer_id>")
@login_required
def detail(customer_id):
    # Buero-Admin und Mitarbeiter (lesend) - nur Kunden des eigenen Bueros (sonst 404).
    customer = get_or_404_scoped(Customer, customer_id)
    is_admin = current_user.is_office_admin

    timeline_events = sorted(customer.timeline_events, key=lambda e: e.occurred_at)
    customer_view = build_customer_detail_context(customer)
    leipziger = customer_entries(customer)
    if not is_admin:
        # Mitarbeiter sehen wie in der Leipziger Liste nur Vorgaenge ihrer eigenen Vermittlernummer;
        # Dubletten vergleichen/zusammenfuehren bleibt Buero-Admins vorbehalten.
        own = vermittlernummer_key(current_user.vermittlernummer)
        leipziger = [i for i in leipziger if own and i["entry"].broker_key == own]
        customer_view["case_rows"] = [r for r in customer_view["case_rows"] if own and vermittlernummer_key(r.get("broker_number")) == own]
        customer_view["possible_duplicates"] = []

    document_customers = (
        DocumentCustomer.query.join(DocumentCustomer.document)
        .filter(DocumentCustomer.customer_id == customer.id)
        .all()
    )
    document_customers.sort(key=lambda dc: dc.document.uploaded_at)

    open_tasks = (
        Task.query.filter(Task.customer_id == customer.id, Task.status == TaskStatus.OPEN)
        .order_by(Task.due_date.asc())
        .all()
    )

    return render_template(
        "customers/detail.html",
        customer=customer,
        timeline_events=timeline_events,
        customer_view=customer_view,
        case_rows=customer_view["case_rows"],
        customer_summary=customer_view["summary"],
        possible_duplicates=customer_view["possible_duplicates"],
        document_customers=document_customers,
        open_tasks=open_tasks,
        leipziger_entries=leipziger,
        memos=customer_memos(customer),
        memo_basis=MEMO_BASIS_LABELS,
        source_labels=customer_sources.LABELS,
        display_name=display_name_for,
        huk_jobs=huk_jobs.jobs_for_customer(current_user, customer.id),
        huk_labels=huk_jobs.STATUS_LABELS,
        huk_tones=huk_jobs.STATUS_TONES,
        # Notizen und offene Rueckrufe aus dem Eingabefeld: dieselbe Quelle wie die Kundenkarte
        # im Assistenten (customer_notes.customer_card), damit beide Einstiege dasselbe zeigen.
        note_card=customer_notes.customer_card(current_user, customer),
        # Bearbeiten direkt am Feld: dieselbe Pruefung wie die Kundenkarte (customer_create.update_fields).
        can_edit=customer_create.can_edit(current_user, customer),
        edit=customer_create.edit_payload(customer),
        # Seitenfenster (js/workspace.js): nur der Inhalt, ohne Seitenleiste.
        panel=request.args.get("panel") == "1",
    )


def _duplicate_pair(customer_id: int, other_id: int):
    """Beide Datensaetze aus dem eigenen Buero (sonst 404) und als Dublette erkannt."""
    customer = get_or_404_scoped(Customer, customer_id)
    other = get_or_404_scoped(Customer, other_id)
    reason = match_reason(customer, other)
    if reason is None:
        abort(404)
    return customer, other, reason


@customers_bp.get("/<int:customer_id>/dublette/<int:other_id>")
@login_required
@admin_required
def compare(customer_id, other_id):
    customer, other, reason = _duplicate_pair(customer_id, other_id)
    return render_template(
        "customers/compare.html",
        customer=customer,
        other=other,
        reason=reason,
        reason_label=REASON_LABELS[reason],
        rows=compare_rows(customer, other),
        counts=(link_counts(customer), link_counts(other)),
    )


@customers_bp.post("/<int:customer_id>/zusammenfuehren/<int:other_id>")
@login_required
@admin_required
def merge(customer_id, other_id):
    """Fuehrt `other` in `customer` zusammen - nur nach ausdruecklicher Bestaetigung."""
    customer, other, _reason = _duplicate_pair(customer_id, other_id)
    if request.form.get("bestaetigt") != "ja":
        flash("Bitte bestätigen, dass es sich eindeutig um denselben Kunden handelt.", "error")
        return redirect(url_for("customers.compare", customer_id=customer.id, other_id=other.id))
    try:
        merge_customers(customer, other, current_user)
    except MergeError as exc:
        flash(str(exc), "error")
        return redirect(url_for("customers.compare", customer_id=customer.id, other_id=other.id))
    flash("Die Datensätze wurden zusammengeführt. Alle Verknüpfungen sind erhalten.", "success")
    return redirect(url_for("customers.detail", customer_id=customer_id))
