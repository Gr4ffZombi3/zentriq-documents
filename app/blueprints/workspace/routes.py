"""Arbeitsplatz-Funktionen fuer Buero-Rollen (Buero-Admin und Mitarbeiter): Schnellsuche,
Formularentwuerfe, gespeicherte Ansichten, Startansicht, Listendichte, "Neu"-Markierungen
und "Was ist neu?".

Jede Route arbeitet ausschliesslich mit den Daten des angemeldeten Benutzers (user_id) im
eigenen Buero; SUPER_ADMIN erreicht den Blueprint nicht (app/auth/permissions.py)."""

from flask import Blueprint, abort, flash, jsonify, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from app.auth.permissions import admin_required
from app.extensions import db
from app.models import OfficeNews
from app.navigation import build_navigation
from app.services import pending_cases, workspace
from app.tenancy import get_or_404_scoped

workspace_bp = Blueprint("workspace", __name__, url_prefix="/arbeitsplatz")


@workspace_bp.before_request
def _office_only():
    # Nicht angemeldet: login_required der Route leitet zur Anmeldung weiter.
    if current_user.is_authenticated and not workspace.is_office_member(current_user):
        abort(403)


def _json_body() -> dict:
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else {}


# --- Schnellsuche -----------------------------------------------------------------------------


@workspace_bp.get("/suche")
@login_required
def quick_search():
    query = (request.args.get("q") or "").strip()[:60]
    pages = workspace.search_pages(build_navigation()["nav_groups"], query)
    customers = workspace.search_customer_hits(current_user, query)
    cases = [
        {"label": case.name, "detail": " · ".join(v for v in (case.vn_number, pending_cases.NOT_LINKED_LABEL if case.is_open else "zugeordnet") if v),
         "url": url_for("workspace.pending_detail", case_id=case.id)}
        for case in pending_cases.search(query, limit=5, include_linked=True)
    ]
    # Kein Kunde gefunden: direkt anbieten, den Vorgang ohne Kundenakte zu erfassen.
    create = url_for("workspace.pending_new", name=query) if len(query) >= 2 and not customers else None
    return jsonify({"pages": pages, "customers": customers, "cases": cases, "create": create})


# --- Formularentwuerfe ------------------------------------------------------------------------


@workspace_bp.route("/entwuerfe/<path:key>", methods=["GET", "PUT", "DELETE"])
@login_required
def draft(key):
    try:
        if request.method == "GET":
            item = workspace.get_draft(current_user, key)
            return jsonify({"draft": workspace.serialize_draft(item) if item else None})
        if request.method == "PUT":
            item = workspace.save_draft(current_user, key, _json_body().get("data"))
            return jsonify({"draft": workspace.serialize_draft(item)})
        workspace.delete_draft(current_user, key)
        return jsonify({"ok": True})
    except workspace.DraftError as exc:
        db.session.rollback()
        return jsonify({"error": str(exc)}), exc.status


# --- Gespeicherte Ansichten -------------------------------------------------------------------


@workspace_bp.post("/ansichten")
@login_required
def save_view():
    page = request.form.get("page", "")
    back = request.form.get("zurueck", "")
    try:
        view = workspace.save_view(current_user, page, request.form.get("name", ""), request.form)
    except workspace.ViewError as exc:
        db.session.rollback()
        flash(str(exc), "error")
        return redirect(back if back.startswith("/") and not back.startswith("//") else url_for("portal.home"))
    flash(f"Ansicht „{view.name}“ gespeichert.", "success")
    return redirect(workspace.view_url(view))


@workspace_bp.get("/ansichten/<int:view_id>")
@login_required
def open_view(view_id):
    view = workspace.own_view(current_user, view_id)
    if view is None or not workspace.page_allowed(current_user, view.page):
        abort(404)
    return redirect(workspace.view_url(view))


@workspace_bp.post("/ansichten/<int:view_id>/loeschen")
@login_required
def delete_view(view_id):
    view = workspace.own_view(current_user, view_id)
    if view is None:
        abort(404)
    page = view.page
    db.session.delete(view)
    db.session.commit()
    flash("Ansicht gelöscht.", "success")
    endpoint = workspace.VIEW_PAGES[page][0] if page in workspace.VIEW_PAGES else "portal.home"
    return redirect(url_for(endpoint))


# --- Startansicht -----------------------------------------------------------------------------


@workspace_bp.post("/startansicht")
@login_required
def start_view():
    if workspace.set_start_view(current_user, request.form.get("start_view", "")):
        flash("Startansicht gespeichert.", "success")
    else:
        flash("Diese Startansicht ist nicht verfügbar.", "error")
    return redirect(url_for("settings.profile") + "#startansicht")


# --- "Neu"-Markierung und Listendichte -------------------------------------------------------


@workspace_bp.post("/neu/<key>/gesehen")
@login_required
def feature_seen(key):
    if not workspace.mark_feature_seen(current_user, key):
        return jsonify({"error": "Unbekannte Neuerung."}), 404
    return jsonify({"ok": True})


@workspace_bp.post("/listendichte")
@login_required
def list_density():
    value = _json_body().get("density") or request.form.get("density", "")
    if not workspace.set_list_density(current_user, value):
        return jsonify({"error": "Diese Darstellung gibt es nicht."}), 400
    return jsonify({"ok": True, "density": value, "label": workspace.DENSITIES[value]})


# --- Vorgaenge ohne Kundenakte ---------------------------------------------------------------

DURATIONS = (15, 30, 45, 60, 90)


def _appointment_data(form) -> dict | None:
    """Terminfelder des Formulars -> Kalenderdaten; None, wenn kein Termin gewuenscht ist."""
    from datetime import datetime, timedelta

    day, clock = (form.get("termin_datum") or "").strip(), (form.get("termin_zeit") or "").strip()
    if not day and not clock:
        return None
    try:
        start = datetime.strptime(f"{day}T{clock}", "%Y-%m-%dT%H:%M")
    except ValueError as exc:
        raise pending_cases.CaseError("Für den Termin bitte Datum und Uhrzeit angeben.") from exc
    minutes = form.get("termin_dauer", type=int) or 30
    if minutes not in DURATIONS:
        minutes = 30
    return {
        "title": form.get("termin_titel", ""),
        "start": start.strftime("%Y-%m-%dT%H:%M"),
        "end": (start + timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M"),
        "assignee_id": form.get("termin_mitarbeiter") or current_user.id,
    }


def _pending_form_context(**extra) -> dict:
    from app.services.calendar import member_payload, office_members

    day, clock = pending_cases.default_slot()
    return {"members": member_payload(office_members()), "durations": DURATIONS, "default_day": day, "default_time": clock, **extra}


def _own_case(case_id: int):
    from app.models import PendingCase

    return get_or_404_scoped(PendingCase, case_id)


@workspace_bp.get("/vorgaenge")
@login_required
def pending_list():
    from app.models import PendingCase

    query = (request.args.get("q") or "").strip()[:60]
    if query:
        cases = pending_cases.search(query, limit=50, include_linked=True)
    else:
        cases = PendingCase.query.filter(PendingCase.status == "open").order_by(PendingCase.created_at.desc()).limit(100).all()
    return render_template("workspace/pending_list.html", cases=cases, q=query)


@workspace_bp.route("/vorgaenge/neu", methods=["GET", "POST"])
@login_required
def pending_new():
    form = request.form if request.method == "POST" else request.args
    if request.method == "POST":
        try:
            appointment = _appointment_data(request.form)
            note_text = (request.form.get("notiz") or "").strip()
            if not note_text and appointment is None:
                raise pending_cases.CaseError("Bitte eine Notiz eingeben oder einen Termin wählen.")
            case, _created = pending_cases.find_or_create(current_user, request.form.get("name"), request.form.get("vn"))
            ref = (request.form.get("client_ref") or "").strip()[:60] or None
            if note_text:
                pending_cases.add_note(current_user, case, note_text, client_ref=f"{ref}-n" if ref else None)
            db.session.commit()
            if appointment is not None:
                pending_cases.book_appointment(current_user, case, appointment, client_ref=f"{ref}-t" if ref else None)
        except pending_cases.CaseError as exc:
            db.session.rollback()
            # Eingaben bleiben stehen; Hinweis direkt ueber dem Formular.
            return render_template("workspace/pending_new.html", form=request.form, error=str(exc), **_pending_form_context()), 400
        done = [part for part, ok in (("Notiz gespeichert", note_text), ("Termin gebucht", appointment)) if ok]
        flash(" und ".join(done) + " – " + pending_cases.NOT_LINKED_LABEL + ".", "success")
        return redirect(url_for("workspace.pending_detail", case_id=case.id))
    return render_template("workspace/pending_new.html", form=form, error=None, **_pending_form_context())


@workspace_bp.get("/vorgaenge/<int:case_id>")
@login_required
def pending_detail(case_id):
    case = _own_case(case_id)
    return render_template(
        "workspace/pending_detail.html",
        case=case,
        events=pending_cases.events_of(case),
        candidates=pending_cases.candidates(case) if case.is_open else [],
        **_pending_form_context(),
    )


@workspace_bp.post("/vorgaenge/<int:case_id>/notiz")
@login_required
def pending_note(case_id):
    case = _own_case(case_id)
    try:
        pending_cases.add_note(current_user, case, request.form.get("notiz", ""), client_ref=request.form.get("client_ref"))
        db.session.commit()
    except pending_cases.CaseError as exc:
        db.session.rollback()
        flash(str(exc), "error")
        return redirect(url_for("workspace.pending_detail", case_id=case.id, notiz=request.form.get("notiz", "")[:2000]) + "#notiz")
    flash("Notiz gespeichert." if case.is_open else "Notiz gespeichert und in der Kundenakte abgelegt.", "success")
    return redirect(url_for("workspace.pending_detail", case_id=case.id))


@workspace_bp.post("/vorgaenge/<int:case_id>/termin")
@login_required
def pending_appointment(case_id):
    case = _own_case(case_id)
    try:
        data = _appointment_data(request.form)
        if data is None:
            raise pending_cases.CaseError("Für den Termin bitte Datum und Uhrzeit angeben.")
        pending_cases.book_appointment(current_user, case, data, client_ref=request.form.get("client_ref"))
    except pending_cases.CaseError as exc:
        db.session.rollback()
        flash(str(exc), "error")
        return redirect(url_for("workspace.pending_detail", case_id=case.id) + "#termin")
    flash("Termin gebucht – er steht im Bürokalender.", "success")
    return redirect(url_for("workspace.pending_detail", case_id=case.id))


@workspace_bp.post("/vorgaenge/<int:case_id>/zuordnen")
@login_required
def pending_link(case_id):
    from app.models import Customer

    case = _own_case(case_id)
    customer = get_or_404_scoped(Customer, request.form.get("customer_id", type=int))
    if pending_cases.link(case, customer, current_user, basis="manual"):
        db.session.commit()
        flash(f"Vorgang mit der Kundenakte „{customer.name}“ verknüpft. Notizen und Termine stehen jetzt dort.", "success")
    else:
        flash("Dieser Vorgang ist bereits einer Kundenakte zugeordnet.", "info")
    return redirect(url_for("workspace.pending_detail", case_id=case.id))


# --- Was ist neu? -----------------------------------------------------------------------------


@workspace_bp.get("/neuigkeiten")
@login_required
def news():
    items = workspace.news_for(current_user)
    workspace.mark_news_seen(current_user)
    editing = None
    office_items = []
    if current_user.is_office_admin:
        office_items = OfficeNews.query.order_by(OfficeNews.published_on.desc(), OfficeNews.id.desc()).all()
        edit_id = request.args.get("bearbeiten", type=int)
        editing = get_or_404_scoped(OfficeNews, edit_id) if edit_id else None
    return render_template(
        "workspace/news.html",
        items=items,
        office_items=office_items,
        editing=editing,
        audiences=workspace.AUDIENCES,
        today=workspace.office_today(),
    )


@workspace_bp.post("/neuigkeiten")
@login_required
@admin_required
def save_news():
    news_id = request.form.get("id", type=int)
    item = get_or_404_scoped(OfficeNews, news_id) if news_id else None
    try:
        workspace.save_office_news(current_user, request.form, item)
    except workspace.NewsError as exc:
        db.session.rollback()
        flash(str(exc), "error")
        return redirect(url_for("workspace.news", bearbeiten=news_id) if news_id else url_for("workspace.news") + "#pflegen")
    flash("Eintrag gespeichert.", "success")
    return redirect(url_for("workspace.news"))


@workspace_bp.post("/neuigkeiten/<int:news_id>/loeschen")
@login_required
@admin_required
def delete_news(news_id):
    item = get_or_404_scoped(OfficeNews, news_id)
    db.session.delete(item)
    db.session.commit()
    flash("Eintrag gelöscht.", "success")
    return redirect(url_for("workspace.news"))
