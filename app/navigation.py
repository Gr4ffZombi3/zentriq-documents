"""Navigation des Portals: eine ruhige Seitenleiste, nach Zweck gruppiert (hoechstens zwei
Ebenen: Gruppe -> Bereich; Unterseiten eines Bereichs erscheinen als Reiter darueber).

Seitenleiste fuer Buero-Rollen (components/app_sidebar.html):
- oben: Assistent (ZentriqAI-Chat, zentraler KI-Einstieg) und Suche
- Hauptbereiche ohne Ueberschrift: Kunden, Kalender, Leipziger Liste (offene Vorgaenge), Memo,
  Zeiterfassung, Uebersicht. Kunden fuehrt OFFICE_ADMIN zum Kundenstamm, Mitarbeiter zur
  Kundensuche (sie sehen wie bisher keine Kundenstammdaten-Liste).
- "Werkzeuge" (aufklappbar): Kfz-Angebot (HUK-Webrechner), Texte formulieren (Textassistent, nur freigegebene Bueros),
  Dokument anonymisieren, Universal-Upload*
- unten "Verwaltung"* (aufklappbar): Mitarbeiter, Benutzerverwaltung, Aktivitaeten
- ganz unten: Profil mit Mein Konto (inkl. Darstellung) und Abmelden.
* nur OFFICE_ADMIN.
SUPER_ADMIN: eigene Plattformnavigation (Uebersicht, Bueros, Benutzer, System, Sicherheit),
unten KI-Einstellungen; keine Fachbereiche.

Die Sichtbarkeit hier ist nur Komfort - die eigentliche Absicherung erfolgt serverseitig in
app/auth/permissions.py (Default-Deny je Rolle) und per @admin_required."""

from dataclasses import dataclass, field

from flask import current_app, request, url_for
from flask_login import current_user

from app.extensions import db


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
    kind: str = "link"
    # Nur fuer Rollen ohne Admin-Rechte (Gegenstueck zu einem admin_only-Bereich).
    staff_only: bool = False
    icon: str = "file-text"


@dataclass(frozen=True)
class NavGroup:
    key: str
    label: str | None  # None: ohne Ueberschrift (Start bzw. Plattform)
    areas: tuple[NavArea, ...]
    admin_only: bool = False
    # Ueberschrift verlinkt auf eine Bereichsseite (Werkzeuge).
    endpoint: str | None = None
    prefixes: tuple[str, ...] = ()
    # "main": Hauptbereiche oben; "tools": aufklappbar in der Mitte; "admin": aufklappbar unten.
    placement: str = "main"


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
            NavArea("platform_home", "Übersicht", "platform.index", ("platform.index",), admin_only=False, icon="layout-dashboard"),
            NavArea("platform_offices", "Büros", "platform.offices", ("platform.office",), admin_only=False, icon="building-2"),
            NavArea("platform_users", "Benutzer", "platform.users", ("platform.user",), admin_only=False, icon="users"),
            NavArea("platform_system", "System", "platform.system", ("platform.system",), admin_only=False, icon="settings"),
            NavArea("platform_security", "Sicherheit", "platform.security", ("platform.security",), admin_only=False, icon="shield"),
        ),
    ),
    NavGroup(
        "zentriq_ai",
        "Verwaltung",
        (NavArea("zentriq_settings", "KI-Einstellungen", "zentriq_admin.settings", ("zentriq_admin.",), admin_only=False, icon="sparkles"),),
        placement="admin",
    ),
)

OFFICE_GROUPS = (
    NavGroup(
        "main",
        None,
        (
            # Zentriq-Kundenstamm (nur OFFICE_ADMIN); Mitarbeiter finden Kunden ueber die Suche.
            NavArea("customers", "Kunden", "customers.list_customers", ("customers.",), admin_only=True, icon="users"),
            NavArea("customer_search", "Kunden", "search.search", ("search.",), admin_only=False, staff_only=True, icon="users"),
            NavArea("calendar", "Kalender", "calendar.index", ("calendar.",), admin_only=False, icon="calendar"),
            NavArea(
                "leipziger",
                "Leipziger Liste",
                "leipziger.index",
                ("leipziger.", "documents.", "upload.", "potenziale.", "tasks.", "recommendations.", "bestand.", "cockpit."),
                admin_only=False,
                items=LEIPZIGER_ITEMS,
                icon="list-todo",
            ),
            NavArea("voice", "Memo", "dashboard.index", ("dashboard.",), admin_only=False, icon="mic"),
            NavArea("time", "Zeiterfassung", "timetracking.index", ("timetracking.",), admin_only=False, items=TIME_ITEMS, icon="clock"),
            NavArea("overview", "Übersicht", "portal.overview", ("portal.overview",), admin_only=False, icon="layout-dashboard"),
        ),
    ),
    NavGroup(
        "tools",
        "Werkzeuge",
        (
            # Textassistent: nur fuer freigegebene Bueros (assistant_allowed), unabhaengig vom
            # API-Key. Der zentrale KI-Einstieg ist der Assistent (ZentriqAI) ganz oben.
            NavArea("assistant", "Texte formulieren", "assistant.index", ("assistant.",), admin_only=False, kind="assistant", icon="pen-line"),
            NavArea("huk", "Kfz-Angebot (HUK)", "huk.index", ("huk.",), admin_only=False, icon="trending-up"),
            NavArea("anonymize", "Anonymisieren", "tools.anonymize_document", ("tools.anonymize",), admin_only=False, icon="shield-check"),
            # Fuer Mitarbeiter waere der Universal-Upload nur ein zweiter Memo-Upload (sie duerfen
            # keine Listen importieren) - deshalb nur fuer Buero-Admins in der Navigation.
            NavArea("intake", "Universal-Upload", "intake.index", ("intake.",), admin_only=True, icon="upload"),
        ),
        endpoint="tools.index",
        prefixes=("tools.index",),
        placement="tools",
    ),
    NavGroup(
        "office",
        "Verwaltung",
        (
            NavArea("staff", "Mitarbeiter", "office.staff", ("office.staff",), admin_only=True, icon="briefcase"),
            NavArea("office_settings", "Benutzerverwaltung", "settings.users", ("settings.users", "settings.user_"), admin_only=True, icon="user-cog"),
            NavArea("activity", "Aktivitäten", "office.activities", ("office.activities",), admin_only=True, icon="history"),
            # Einreichen fuer alle Buero-Mitglieder; Inhalte sieht nur der Buero-Admin (Blueprint prueft).
            NavArea("settlements", "Abrechnungen", "settlements.index", ("settlements.",), admin_only=False, icon="file-text"),
        ),
        admin_only=False,
        placement="admin",
    ),
)

ACCOUNT_AREA = NavArea("settings", "Mein Konto", "settings.profile", ("settings.profile", "settings.security", "settings.index"), admin_only=False)


def _matches(endpoint: str | None, prefixes: tuple[str, ...]) -> bool:
    return bool(endpoint) and any(endpoint.startswith(prefix) for prefix in prefixes)


def _visible(entry, is_admin: bool) -> bool:
    return (is_admin or not entry.admin_only) and entry.endpoint in current_app.view_functions


def build_navigation() -> dict:
    if not current_user.is_authenticated:
        return {"nav_groups": [], "nav_active_area": None, "nav_subitems": [], "nav_settings_label": "Mein Konto", "nav_assistant_active": False}
    is_admin = current_user.is_admin
    endpoint = request.endpoint
    # Benutzerformulare, die von der Mitarbeiterseite aus geoeffnet wurden.
    from_staff = bool(endpoint and endpoint.startswith("settings.user") and request.values.get("von") == "mitarbeiter")
    has_assistant = assistant_available(current_user)
    show_assistant = assistant_allowed(current_user)
    groups = []
    active_area = None
    for group in PLATFORM_GROUPS if current_user.is_super_admin else OFFICE_GROUPS:
        if group.admin_only and not is_admin:
            continue
        entries = []
        for area in group.areas:
            if area.kind == "assistant" and not show_assistant:
                continue
            if area.staff_only and is_admin:
                continue
            if not _visible(area, is_admin):
                continue
            active = active_area is None and _matches(endpoint, area.prefixes)
            if from_staff and area.key in ("staff", "office_settings"):
                active = area.key == "staff"
            entries.append(
                {"label": area.label, "url": url_for(area.endpoint), "active": active, "key": area.key, "kind": area.kind, "icon": area.icon}
            )
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
                "placement": group.placement,
                # Aufgeklappt, wenn die aktuelle Seite darin liegt.
                "open": _matches(endpoint, group.prefixes) or any(entry["active"] for entry in entries),
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
        # ZentriqAI-Chat (zentraler Assistent) ist keine NavArea: eigener Eintrag ganz oben.
        "nav_assistant_active": bool(endpoint and endpoint.startswith("zentriq.")),
        "assistant_available": has_assistant,
    }



def assistant_allowed(user) -> bool:
    """Darf `user` den Textassistenten verwenden? Nur Buero-Admin und Mitarbeiter eines Bueros,
    fuer das der Assistent freigegeben ist (Tenant.assistant_enabled). SUPER_ADMIN nie. Haengt
    bewusst NICHT vom API-Key ab - ohne Key zeigt die Seite nur einen Hinweis."""
    if not getattr(user, "is_authenticated", False) or not (user.is_office_admin or user.is_employee):
        return False
    from app.models import Tenant

    tenant = db.session.get(Tenant, user.tenant_id) if user.tenant_id else None
    return bool(tenant is not None and tenant.assistant_enabled)


def assistant_available(user) -> bool:
    """Assistent erlaubt UND technisch einsatzbereit (API-Key gesetzt, nicht abgeschaltet) -
    fuer Zusatzfunktionen wie die Memo-Kurzfassung, die nur mit Antwort sinnvoll sind."""
    from app.services.assistant import is_enabled

    return assistant_allowed(user) and is_enabled()


def display_name_for(user) -> str:
    profile = None if user.is_super_admin else user.employee_profile
    if profile is not None and profile.display_name:
        return profile.display_name
    return user.email


def home_endpoint_for(user) -> str:
    """Startseite nach dem Login: fuer alle Rollen der ZentriqAI-Chat. Ist ZentriqAI abgeschaltet,
    wie bisher Plattformverwaltung bzw. persoenliche Uebersicht (Fallback: Profil)."""
    from app.services.workspace import start_endpoint

    chosen = start_endpoint(user)  # persoenliche Startansicht (Mein Konto), sofern erlaubt
    if chosen:
        return chosen
    candidates = ("platform.index",) if user.is_super_admin else ("portal.overview",)
    if current_app.config.get("ZENTRIQ_AI_ENABLED", True):
        candidates = ("zentriq.index", *candidates)
    for endpoint in (*candidates, "timetracking.index", "settings.profile"):
        if endpoint in current_app.view_functions:
            return endpoint
    return "settings.profile"
