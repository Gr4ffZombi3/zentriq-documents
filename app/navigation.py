"""Navigation des Portals: eine ruhige Seitenleiste, nach Taetigkeit gruppiert (hoechstens
zwei Ebenen: Gruppe -> Bereich; Unterseiten eines Bereichs erscheinen als Reiter darueber).

- Buero-Rollen: Start | Arbeit (Leipziger Liste, Memo, Kunden*) | Zeit (Zeiterfassung) |
  Werkzeuge (Assistent, Dokument anonymisieren, Universal-Upload*) | Buero* (Mitarbeiter,
  Benutzerverwaltung, Aktivitaeten). * nur OFFICE_ADMIN.
- SUPER_ADMIN: eigene Plattformnavigation (Uebersicht, Bueros, Benutzer, System, Sicherheit),
  keine Fachbereiche.
Unten in der Leiste fuer alle: Name, Rolle, Mein Konto, Abmelden.

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
    # "assistant": kein Link, sondern der Schalter fuer das Assistent-Panel.
    kind: str = "link"


@dataclass(frozen=True)
class NavGroup:
    key: str
    label: str | None  # None: ohne Ueberschrift (Start bzw. Plattform)
    areas: tuple[NavArea, ...]
    admin_only: bool = False
    # Ueberschrift verlinkt auf eine Bereichsseite (Werkzeuge).
    endpoint: str | None = None
    prefixes: tuple[str, ...] = ()


LEIPZIGER_ITEMS = (
    NavItem("leipziger.index", "Zu erledigen", ("leipziger.index",)),
    NavItem("leipziger.team", "Mitarbeiter", ("leipziger.team",), admin_only=True),
    NavItem("documents.list_documents", "Listen & Upload", ("documents.", "upload."), admin_only=True),
    NavItem("potenziale.index", "Auswertung", ("potenziale.index",), admin_only=True),
    NavItem("potenziale.vergleich", "Eigene vs. GS-Liste", ("potenziale.vergleich",), admin_only=True, secondary=True),
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

PLATFORM_GROUPS = (
    NavGroup(
        "platform",
        None,
        (
            NavArea("platform_home", "Übersicht", "platform.index", ("platform.index",), admin_only=False),
            NavArea("platform_offices", "Büros", "platform.offices", ("platform.office",), admin_only=False),
            NavArea("platform_users", "Benutzer", "platform.users", ("platform.user",), admin_only=False),
            NavArea("platform_system", "System", "platform.system", ("platform.system",), admin_only=False),
            NavArea("platform_security", "Sicherheit", "platform.security", ("platform.security",), admin_only=False),
        ),
    ),
)

OFFICE_GROUPS = (
    NavGroup("start", None, (NavArea("overview", "Start", "portal.overview", ("portal.overview",), admin_only=False),)),
    NavGroup(
        "work",
        "Arbeit",
        (
            NavArea(
                "leipziger",
                "Leipziger Liste",
                "leipziger.index",
                ("leipziger.", "documents.", "upload.", "potenziale.", "tasks.", "recommendations.", "bestand.", "cockpit."),
                admin_only=False,
                items=LEIPZIGER_ITEMS,
            ),
            NavArea("voice", "Memo", "dashboard.index", ("dashboard.",), admin_only=False),
            # Zentriq-Kundenstamm (nur OFFICE_ADMIN; Mitarbeiter sehen wie bisher keine Kundenstammdaten).
            NavArea("customers", "Kunden", "customers.list_customers", ("customers.",), admin_only=True),
        ),
    ),
    NavGroup("time", "Zeit", (NavArea("time", "Zeiterfassung", "timetracking.index", ("timetracking.",), admin_only=False, items=TIME_ITEMS),)),
    NavGroup(
        "tools",
        "Werkzeuge",
        (
            NavArea("assistant", "Assistent", "assistant.generate", (), admin_only=False, kind="assistant"),
            NavArea("anonymize", "Dokument anonymisieren", "tools.anonymize_document", ("tools.anonymize",), admin_only=False),
            # Fuer Mitarbeiter waere der Universal-Upload nur ein zweiter Memo-Upload (sie duerfen
            # keine Listen importieren) - deshalb nur fuer Buero-Admins in der Navigation.
            NavArea("intake", "Universal-Upload", "intake.index", ("intake.",), admin_only=True),
        ),
        endpoint="tools.index",
        prefixes=("tools.index",),
    ),
    NavGroup(
        "office",
        "Büro",
        (
            NavArea("staff", "Mitarbeiter", "office.staff", ("office.staff",), admin_only=True),
            NavArea("office_settings", "Benutzerverwaltung", "settings.users", ("settings.users", "settings.user_"), admin_only=True),
            NavArea("activity", "Aktivitäten", "office.activities", ("office.activities",), admin_only=True),
        ),
        admin_only=True,
    ),
)

ACCOUNT_AREA = NavArea("settings", "Mein Konto", "settings.profile", ("settings.profile", "settings.security", "settings.index"), admin_only=False)


def _matches(endpoint: str | None, prefixes: tuple[str, ...]) -> bool:
    return bool(endpoint) and any(endpoint.startswith(prefix) for prefix in prefixes)


def _visible(entry, is_admin: bool) -> bool:
    return (is_admin or not entry.admin_only) and entry.endpoint in current_app.view_functions


def build_navigation() -> dict:
    if not current_user.is_authenticated:
        return {"nav_groups": [], "nav_active_area": None, "nav_subitems": [], "nav_settings_label": "Mein Konto"}
    is_admin = current_user.is_admin
    endpoint = request.endpoint
    # Benutzerformulare, die von der Mitarbeiterseite aus geoeffnet wurden.
    from_staff = bool(endpoint and endpoint.startswith("settings.user") and request.values.get("von") == "mitarbeiter")
    has_assistant = assistant_available(current_user)
    groups = []
    active_area = None
    for group in PLATFORM_GROUPS if current_user.is_super_admin else OFFICE_GROUPS:
        if group.admin_only and not is_admin:
            continue
        entries = []
        for area in group.areas:
            if area.kind == "assistant":
                if has_assistant:
                    entries.append({"label": area.label, "url": None, "active": False, "key": area.key, "kind": area.kind})
                continue
            if not _visible(area, is_admin):
                continue
            active = active_area is None and _matches(endpoint, area.prefixes)
            if from_staff and area.key in ("staff", "office_settings"):
                active = area.key == "staff"
            entries.append({"label": area.label, "url": url_for(area.endpoint), "active": active, "key": area.key, "kind": area.kind})
            if active:
                active_area = area
        if not entries:
            continue
        heading_url = url_for(group.endpoint) if group.endpoint and group.endpoint in current_app.view_functions else None
        groups.append(
            {
                "key": group.key,
                "label": group.label,
                "url": heading_url,
                "active": _matches(endpoint, group.prefixes),
                "entries": entries,
            }
        )
    account_active = active_area is None and _matches(endpoint, ACCOUNT_AREA.prefixes)

    subitems = []
    more_items = []
    if active_area is not None:
        for item in active_area.items:
            if _visible(item, is_admin):
                entry = {"label": item.label, "url": url_for(item.endpoint), "active": _matches(endpoint, item.prefixes)}
                (more_items if item.secondary else subitems).append(entry)
    return {
        "nav_groups": groups,
        "nav_active_area": active_area.key if active_area else None,
        "nav_active_label": active_area.label if active_area else None,
        "nav_subitems": subitems,
        "nav_more_items": more_items,
        "nav_more_active": any(item["active"] for item in more_items),
        "nav_settings_active": account_active,
        "nav_settings_label": "Mein Konto",
        "nav_display_name": display_name_for(current_user),
        "assistant_available": has_assistant,
    }


def assistant_panel_actions() -> list[tuple[str, str]]:
    from app.services.assistant import ACTIONS, PANEL_ACTIONS

    return [(key, ACTIONS[key].label) for key in PANEL_ACTIONS]


def assistant_available(user) -> bool:
    """KI-Assistent nur fuer Buero-Rollen und nur, wenn er eingerichtet und aktiviert ist."""
    from app.services.assistant import is_enabled

    return bool((user.is_office_admin or user.is_employee) and is_enabled())


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
