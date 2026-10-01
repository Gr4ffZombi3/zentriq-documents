"""Hauptnavigation des Portals: Leipziger Liste, Memo, Zeiterfassung plus Einstellungen,
rollenabhaengig.

Die Sichtbarkeit hier ist nur Komfort - die eigentliche Absicherung erfolgt serverseitig in
app/auth/permissions.py (Default-Deny fuer Mitarbeiter) und per @admin_required."""

from dataclasses import dataclass, field

from flask import current_app, request, url_for
from flask_login import current_user


@dataclass(frozen=True)
class NavItem:
    endpoint: str
    label: str
    prefixes: tuple[str, ...]
    admin_only: bool = False
    # Selten genutzte Ansichten erscheinen gesammelt unter "Weitere" statt als eigener Reiter.
    secondary: bool = False


@dataclass(frozen=True)
class NavArea:
    key: str
    label: str
    endpoint: str
    prefixes: tuple[str, ...]
    admin_only: bool
    items: tuple[NavItem, ...] = field(default_factory=tuple)


LEIPZIGER_ITEMS = (
    NavItem("leipziger.index", "Zu erledigen", ("leipziger.index",)),
    NavItem("leipziger.team", "Mitarbeiter", ("leipziger.team",), admin_only=True),
    NavItem("documents.list_documents", "Listen & Upload", ("documents.", "upload."), admin_only=True),
    NavItem("potenziale.index", "Auswertung", ("potenziale.index",), admin_only=True),
    NavItem("potenziale.vergleich", "Eigene vs. GS-Liste", ("potenziale.vergleich",), admin_only=True, secondary=True),
    NavItem("customers.list_customers", "Kunden", ("customers.",), admin_only=True, secondary=True),
    NavItem("tasks.list_tasks", "Aufgaben", ("tasks.",), admin_only=True, secondary=True),
    NavItem("recommendations.list_recommendations", "Empfehlungen", ("recommendations.",), admin_only=True, secondary=True),
    NavItem("bestand.index", "Bestand", ("bestand.",), admin_only=True, secondary=True),
    NavItem("cockpit.index", "Cockpit", ("cockpit.",), admin_only=True, secondary=True),
)

VOICE_ITEMS = (NavItem("dashboard.index", "Eingang", ("dashboard.", "mailbox.")),)

TIME_ITEMS = (
    NavItem("timetracking.index", "Heute", ("timetracking.index",)),
    NavItem("timetracking.week", "Woche", ("timetracking.week",)),
    NavItem("timetracking.month", "Monat", ("timetracking.month",)),
    NavItem("timetracking.my_requests", "Meine Korrekturanträge", ("timetracking.my_requests", "timetracking.new_request")),
    NavItem("timetracking.team", "Team", ("timetracking.team", "timetracking.employee", "timetracking.session_", "timetracking.break_"), admin_only=True),
    NavItem("timetracking.requests", "Anträge", ("timetracking.requests", "timetracking.decide_request"), admin_only=True),
    NavItem("timetracking.audit", "Protokoll", ("timetracking.audit",), admin_only=True),
)

SETTINGS_ITEMS = (
    NavItem("settings.profile", "Profil", ("settings.profile",)),
    NavItem("settings.users", "Benutzer", ("settings.users", "settings.user_"), admin_only=True),
)

AREAS = (
    NavArea(
        "leipziger",
        "Leipziger Liste",
        "leipziger.index",
        ("leipziger.", "documents.", "upload.", "potenziale.", "customers.", "tasks.", "recommendations.", "bestand.", "cockpit.", "search."),
        admin_only=False,
        items=LEIPZIGER_ITEMS,
    ),
    NavArea("voice", "Memo", "dashboard.index", ("dashboard.", "mailbox."), admin_only=True, items=VOICE_ITEMS),
    NavArea("time", "Zeiterfassung", "timetracking.index", ("timetracking.",), admin_only=False, items=TIME_ITEMS),
)

SETTINGS_AREA = NavArea("settings", "Einstellungen", "settings.index", ("settings.",), admin_only=False, items=SETTINGS_ITEMS)


def _matches(endpoint: str | None, prefixes: tuple[str, ...]) -> bool:
    return bool(endpoint) and any(endpoint.startswith(prefix) for prefix in prefixes)


def _visible(entry, is_admin: bool) -> bool:
    return (is_admin or not entry.admin_only) and entry.endpoint in current_app.view_functions


def build_navigation() -> dict:
    if not current_user.is_authenticated:
        return {"nav_areas": [], "nav_active_area": None, "nav_subitems": []}
    is_admin = current_user.is_admin
    endpoint = request.endpoint
    areas = []
    active_area = None
    for area in AREAS:
        if not _visible(area, is_admin):
            continue
        active = _matches(endpoint, area.prefixes)
        areas.append({"label": area.label, "url": url_for(area.endpoint), "active": active, "key": area.key})
        if active:
            active_area = area
    if active_area is None and _matches(endpoint, SETTINGS_AREA.prefixes):
        active_area = SETTINGS_AREA

    subitems = []
    more_items = []
    if active_area is not None:
        for item in active_area.items:
            if _visible(item, is_admin):
                entry = {"label": item.label, "url": url_for(item.endpoint), "active": _matches(endpoint, item.prefixes)}
                (more_items if item.secondary else subitems).append(entry)
    return {
        "nav_areas": areas,
        "nav_active_area": active_area.key if active_area else None,
        "nav_active_label": active_area.label if active_area else None,
        "nav_subitems": subitems,
        "nav_more_items": more_items,
        "nav_more_active": any(item["active"] for item in more_items),
        "nav_settings_active": active_area is SETTINGS_AREA,
    }


def home_endpoint_for(user) -> str:
    """Startseite nach dem Login: Admins landen in der Leipziger Liste, Mitarbeiter in der
    Zeiterfassung (Fallback: Profil, falls ein Bereich nicht registriert ist)."""
    candidates = ("leipziger.index",) if user.is_admin else ()
    for endpoint in (*candidates, "timetracking.index", "settings.profile"):
        if endpoint in current_app.view_functions:
            return endpoint
    return "settings.profile"
