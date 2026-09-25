"""Rollenbasierte Zugriffskontrolle (serverseitig, nicht nur im Menue).

Default-Deny fuer Mitarbeiter: ein MITARBEITER erreicht ausschliesslich die unten
freigegebenen Blueprints/Endpunkte. Jeder andere Endpunkt - auch per direkter URL, HTMX-
oder JSON-Aufruf - endet mit 403. Neue Blueprints sind damit automatisch nur fuer Admins
erreichbar, solange sie hier nicht ausdruecklich freigegeben werden."""

from functools import wraps

from flask import abort, request
from flask_login import current_user

# Blueprints, die Mitarbeiter vollstaendig erreichen duerfen (Admin-Unterseiten der
# Zeiterfassung sind dort zusaetzlich per @admin_required abgesichert).
EMPLOYEE_ALLOWED_BLUEPRINTS = frozenset({"auth", "timetracking"})
EMPLOYEE_ALLOWED_ENDPOINTS = frozenset({"static", "portal.home", "settings.index", "settings.profile"})


def is_endpoint_allowed_for_employee(endpoint: str | None, blueprint: str | None) -> bool:
    if endpoint is None:
        # Kein Routing-Treffer -> Flask liefert ohnehin 404.
        return True
    return blueprint in EMPLOYEE_ALLOWED_BLUEPRINTS or endpoint in EMPLOYEE_ALLOWED_ENDPOINTS


def enforce_role_access():
    """before_request-Hook: sperrt Admin-Bereiche fuer eingeloggte Mitarbeiter."""
    if not current_user.is_authenticated or current_user.is_admin:
        return None
    if not is_endpoint_allowed_for_employee(request.endpoint, request.blueprint):
        abort(403)
    return None


def admin_required(view):
    """Zusaetzliche, explizite Absicherung einzelner Admin-Views (z. B. innerhalb der
    Zeiterfassung, die Mitarbeitern grundsaetzlich offensteht)."""

    @wraps(view)
    def wrapped(*args, **kwargs):
        if not current_user.is_authenticated:
            abort(401)
        if not current_user.is_admin:
            abort(403)
        return view(*args, **kwargs)

    return wrapped
