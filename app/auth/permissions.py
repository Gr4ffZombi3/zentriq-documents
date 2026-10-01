"""Rollenbasierte Zugriffskontrolle (serverseitig, nicht nur im Menue).

Default-Deny je Rolle: EMPLOYEE und SUPER_ADMIN erreichen ausschliesslich die unten
freigegebenen Blueprints/Endpunkte. Jeder andere Endpunkt - auch per direkter URL, HTMX-
oder JSON-Aufruf - endet mit 403. Neue Blueprints sind damit automatisch nur fuer
OFFICE_ADMINs erreichbar, solange sie hier nicht ausdruecklich freigegeben werden.

- EMPLOYEE: Uebersicht, eigene Zeiterfassung, eigene Eintraege der Leipziger Liste, Memo,
  globale Suche (nur eigene Vorgaenge), eigenes Konto.
- OFFICE_ADMIN: alle Fachbereiche des eigenen Mandanten (Mandantentrennung: app/tenancy.py),
  aber nie die Plattformverwaltung.
- SUPER_ADMIN: ausschliesslich Plattformverwaltung (Bueros, Benutzer) und eigenes Konto -
  KEINE fachlichen Buerodaten (Leipziger Liste, Memo, Zeiterfassung, Dokumente ...).
"""

from functools import wraps

from flask import abort, current_app, redirect, request, url_for
from flask_login import current_user

# Fuer jeden angemeldeten Benutzer erreichbar (Anmeldung, eigenes Konto, 2FA-Einrichtung).
# "pwa": Manifest, Service Worker und Offline-Seite (ohne Benutzerdaten).
COMMON_ALLOWED_BLUEPRINTS = frozenset({"auth", "pwa"})
COMMON_ALLOWED_ENDPOINTS = frozenset(
    {
        "static",
        "favicon",
        "portal.home",
        "settings.index",
        "settings.profile",
        "settings.security",
        "settings.revoke_other_sessions",
    }
)

# Blueprints, die Mitarbeiter vollstaendig erreichen duerfen (Admin-Unterseiten der
# Zeiterfassung sind dort zusaetzlich per @admin_required abgesichert).
EMPLOYEE_ALLOWED_BLUEPRINTS = frozenset({"timetracking"})
# "leipziger.index" zeigt Mitarbeitern ausschliesslich die ueber ihre eigene Vermittlernummer
# zugeordneten Vorgaenge. Memo (dashboard.*) speichert nichts; der Kundenabgleich bleibt im
# eigenen Mandanten.
EMPLOYEE_ALLOWED_ENDPOINTS = frozenset(
    {
        "portal.overview",
        "leipziger.index",
        "dashboard.index",
        "dashboard.transcribe",
        "dashboard.match",
        # Globale Suche: Mitarbeiter finden nur Vorgaenge ihrer eigenen Vermittlernummer
        # (app/services/global_search.py).
        "search.search",
    }
)

SUPER_ADMIN_ALLOWED_BLUEPRINTS = frozenset({"platform"})
SUPER_ADMIN_ALLOWED_ENDPOINTS = frozenset()

# Nur fuer SUPER_ADMIN - auch ein OFFICE_ADMIN erreicht diese nie.
SUPER_ADMIN_ONLY_BLUEPRINTS = frozenset({"platform"})

# Waehrend die 2FA-Einrichtung erzwungen wird, sind nur diese Endpunkte erreichbar.
TWO_FACTOR_SETUP_ENDPOINTS = frozenset(
    {"static", "favicon", "settings.security", "auth.logout", "pwa.manifest", "pwa.service_worker", "pwa.offline"}
)


def _is_common(endpoint: str | None, blueprint: str | None) -> bool:
    return blueprint in COMMON_ALLOWED_BLUEPRINTS or endpoint in COMMON_ALLOWED_ENDPOINTS


def is_endpoint_allowed_for_employee(endpoint: str | None, blueprint: str | None) -> bool:
    if endpoint is None:
        # Kein Routing-Treffer -> Flask liefert ohnehin 404.
        return True
    return (
        _is_common(endpoint, blueprint)
        or blueprint in EMPLOYEE_ALLOWED_BLUEPRINTS
        or endpoint in EMPLOYEE_ALLOWED_ENDPOINTS
    )


def is_endpoint_allowed_for_super_admin(endpoint: str | None, blueprint: str | None) -> bool:
    if endpoint is None:
        return True
    return (
        _is_common(endpoint, blueprint)
        or blueprint in SUPER_ADMIN_ALLOWED_BLUEPRINTS
        or endpoint in SUPER_ADMIN_ALLOWED_ENDPOINTS
    )


def is_endpoint_allowed_for_office_admin(endpoint: str | None, blueprint: str | None) -> bool:
    if endpoint is None:
        return True
    return blueprint not in SUPER_ADMIN_ONLY_BLUEPRINTS


def is_endpoint_allowed(user, endpoint: str | None, blueprint: str | None) -> bool:
    if user.is_super_admin:
        return is_endpoint_allowed_for_super_admin(endpoint, blueprint)
    if user.is_office_admin:
        return is_endpoint_allowed_for_office_admin(endpoint, blueprint)
    if user.is_employee:
        return is_endpoint_allowed_for_employee(endpoint, blueprint)
    return False


def enforce_role_access():
    """before_request-Hook: erzwingt 2FA-Einrichtung und die Bereichsfreigaben je Rolle."""
    if not current_user.is_authenticated:
        return None
    endpoint = request.endpoint
    if (
        current_app.config.get("TWO_FACTOR_ENFORCED")
        and not current_user.two_factor_enabled
        and endpoint is not None
        and endpoint not in TWO_FACTOR_SETUP_ENDPOINTS
    ):
        if request.method == "GET" and not request.headers.get("HX-Request"):
            return redirect(url_for("settings.security"))
        abort(403)
    if not is_endpoint_allowed(current_user, endpoint, request.blueprint):
        abort(403)
    return None


def admin_required(view):
    """Explizite Absicherung einzelner Buero-Admin-Views (z. B. innerhalb der Zeiterfassung,
    die Mitarbeitern grundsaetzlich offensteht). SUPER_ADMIN ist hier bewusst ausgeschlossen."""

    @wraps(view)
    def wrapped(*args, **kwargs):
        if not current_user.is_authenticated:
            abort(401)
        if not current_user.is_office_admin:
            abort(403)
        return view(*args, **kwargs)

    return wrapped


office_admin_required = admin_required


def super_admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not current_user.is_authenticated:
            abort(401)
        if not current_user.is_super_admin:
            abort(403)
        return view(*args, **kwargs)

    return wrapped
