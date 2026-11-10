"""Arbeitsplatz-Funktionen fuer Buero-Rollen (Buero-Admin und Mitarbeiter): Schnellsuche,
Formularentwuerfe, gespeicherte Ansichten, Startansicht und "Was ist neu?".

Jede Route arbeitet ausschliesslich mit den Daten des angemeldeten Benutzers (user_id) im
eigenen Buero; SUPER_ADMIN erreicht den Blueprint nicht (app/auth/permissions.py)."""

from flask import Blueprint, abort, flash, jsonify, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from app.auth.permissions import admin_required
from app.extensions import db
from app.models import OfficeNews
from app.navigation import build_navigation
from app.services import workspace
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
    return jsonify({"pages": pages, "customers": customers})


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
