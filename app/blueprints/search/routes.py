"""Globale Suche (Kopfleiste). Rechte und Umfang: app/services/global_search.py."""

from flask import Blueprint, abort, render_template, request
from flask_login import current_user, login_required

from app.services.global_search import MIN_QUERY_LENGTH, global_search

search_bp = Blueprint("search", __name__, url_prefix="/search")


@search_bp.route("")
@login_required
def search():
    # Nur Buero-Mitglieder: der Plattformbetreiber sieht keine Buerodaten (zusaetzlich zum
    # Default-Deny in app/auth/permissions.py).
    if not (current_user.is_office_admin or current_user.is_employee):
        abort(403)
    results = global_search(current_user, request.args.get("q"))
    from app.services import pending_cases

    pending = pending_cases.search(results.query, include_linked=True) if len(results.query) >= MIN_QUERY_LENGTH else []
    return render_template("search/results.html", results=results, min_length=MIN_QUERY_LENGTH, pending=pending)
