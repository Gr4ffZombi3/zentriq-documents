"""Hauptnavigation des Portals, rollenabhaengig:

- SUPER_ADMIN: Uebersicht, Bueros, Benutzer, Systemeinstellungen, Sicherheit (keine Fachbereiche).
- OFFICE_ADMIN: Uebersicht, Leipziger Liste, Memo, Zeiterfassung, Mitarbeiter, Aktivitaeten,
  Einstellungen.
- EMPLOYEE: Uebersicht, Leipziger Liste, Memo, Zeiterfassung.
Rechts in der Kopfleiste fuer alle: Mein Konto, Benutzername, Abmelden.

Die Sichtbarkeit hier ist nur Komfort - die eigentliche Absicherung erfolgt serverseitig in
app/auth/permissions.py (Default-Deny je Rolle) und per @admin_required."""

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

TIME_ITEMS = (
    NavItem("timetracking.index", "Heute", ("timetracking.index",)),
    NavItem("timetracking.week", "Woche", ("timetracking.week",)),
    NavItem("timetracking.month", "Monat", ("timetracking.month",)),
    NavItem("timetracking.my_requests", "Meine Korrekturanträge", ("timetracking.my_requests", "timetracking.new_request")),
    NavItem("timetracking.team", "Team", ("timetracking.team", "timetracking.employee", "timetracking.session_", "timetracking.break_"), admin_only=True),
    NavItem("timetracking.requests", "Anträge", ("timetracking.requests", "timetracking.decide_request"), admin_only=True),
    NavItem("timetracking.audit", "Protokoll", ("timetracking.audit",), admin_only=True),
)

PLATFORM_AREAS = (
    NavArea("platform_home", "Übersicht", "platform.index", ("platform.index",), admin_only=False),
    NavArea("platform_offices", "Büros", "platform.offices", ("platform.office",), admin_only=False),
    NavArea("platform_users", "Benutzer", "platform.users", ("platform.user",), admin_only=False),
    NavArea("platform_system", "Systemeinstellungen", "platform.system", ("platform.system",), admin_only=False),
    NavArea("platform_security", "Sicherheit", "platform.security", ("platform.security",), admin_only=False),
)

AREAS = (
    NavArea("overview", "Übersicht", "portal.overview", ("portal.overview",), admin_only=False),
    NavArea(
        "leipziger",
        "Leipziger Liste",
        "leipziger.index",
        ("leipziger.", "documents.", "upload.", "potenziale.", "customers.", "tasks.", "recommendations.", "bestand.", "cockpit."),
        admin_only=False,
        items=LEIPZIGER_ITEMS,
    ),
    NavArea("voice", "Memo", "dashboard.index", ("dashboard.",), admin_only=False),
    NavArea("time", "Zeiterfassung", "timetracking.index", ("timetracking.",), admin_only=False, items=TIME_ITEMS),
    # Buero-Verwaltung (nur OFFICE_ADMIN).
    NavArea("staff", "Mitarbeiter", "office.staff", ("office.staff",), admin_only=True),
    NavArea("activity", "Aktivitäten", "office.activities", ("office.activities",), admin_only=True),
    NavArea("office_settings", "Einstellungen", "settings.users", ("settings.users", "settings.user_"), admin_only=True),
)

ACCOUNT_AREA = NavArea("settings", "Mein Konto", "settings.profile", ("settings.profile", "settings.security", "settings.index"), admin_only=False)


def _matches(endpoint: str | None, prefixes: tuple[str, ...]) -> bool:
    return bool(endpoint) and any(endpoint.startswith(prefix) for prefix in prefixes)


def _visible(entry, is_admin: bool) -> bool:
    return (is_admin or not entry.admin_only) and entry.endpoint in current_app.view_functions


def build_navigation() -> dict:
    if not current_user.is_authenticated:
        return {"nav_areas": [], "nav_active_area": None, "nav_subitems": [], "nav_settings_label": "Mein Konto"}
    is_admin = current_user.is_admin
    endpoint = request.endpoint
    # Benutzerformulare, die von der Mitarbeiterseite aus geoeffnet wurden.
    from_staff = bool(endpoint and endpoint.startswith("settings.user") and request.values.get("von") == "mitarbeiter")
    areas = []
    active_area = None
    for area in PLATFORM_AREAS if current_user.is_super_admin else AREAS:
        if not _visible(area, is_admin):
            continue
        active = active_area is None and _matches(endpoint, area.prefixes)
        if from_staff and area.key in ("staff", "office_settings"):
            active = area.key == "staff"
        areas.append({"label": area.label, "url": url_for(area.endpoint), "active": active, "key": area.key})
        if active:
            active_area = area
    account_active = active_area is None and _matches(endpoint, ACCOUNT_AREA.prefixes)

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
        "nav_settings_active": account_active,
        "nav_settings_label": "Mein Konto",
        "nav_display_name": display_name_for(current_user),
    }


def display_name_for(user) -> str:
    profile = None if user.is_super_admin else user.employee_profile
    if profile is not None and profile.display_name:
        return profile.display_name
    return user.email


def home_endpoint_for(user) -> str:
    """Startseite nach dem Login: Super-Admins landen in der Bueroverwaltung, alle Buero-Rollen
    auf ihrer persoenlichen Uebersicht (Fallback: Profil, falls ein Bereich nicht registriert ist)."""
    candidates = ("platform.index",) if user.is_super_admin else ("portal.overview",)
    for endpoint in (*candidates, "timetracking.index", "settings.profile"):
        if endpoint in current_app.view_functions:
            return endpoint
    return "settings.profile"
