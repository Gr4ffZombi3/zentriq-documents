"""ZentriqAI-Werkzeuge: Zugriff auf bestehende Buerofunktionen per natuerlicher Sprache.

Grundsaetze
- Jedes Werkzeug ruft ausschliesslich BESTEHENDE Services auf (keine parallelen Geschaefts-
  prozesse) und laeuft im Mandantenkontext des Benutzers (TenantScopedMixin filtert automatisch).
- Rechte: Ein Werkzeug ist an den Endpunkt gebunden, der dieselben Daten in der Oberflaeche
  zeigt. Es steht nur zur Verfuegung, wenn app/auth/permissions.is_endpoint_allowed() diesen
  Endpunkt fuer den Benutzer erlaubt (plus ggf. Buero-Admin-Pflicht wie @admin_required).
  Geprueft wird beim Anbieten UND erneut beim Ausfuehren.
- Derzeit nur LESENDE Werkzeuge. Schreibende/externe Werkzeuge erhalten eine Bestaetigung
  durch den Benutzer, bevor sie ausgefuehrt werden (siehe docs/zentriq-ai-konzept.md).
- Ergebnisse sind knapp (begrenzte Zeilen) und werden dem Modell als Daten uebergeben.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, time
from typing import Callable

from flask import current_app

from app.auth.permissions import is_endpoint_allowed
from app.services.timetracking.calc import format_duration

MAX_ROWS = 25
MAX_RESULT_CHARS = 6000


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    parameters: dict
    endpoint: str
    handler: Callable
    office_admin_only: bool = False
    label: str = ""

    def schema(self) -> dict:
        return {"name": self.name, "description": self.description, "parameters": self.parameters}


def _fmt_dt(value) -> str | None:
    from app.services.timetracking.clock import to_local

    local = to_local(value)
    return local.strftime("%d.%m.%Y %H:%M") if local else None


# --- Handler ---------------------------------------------------------------------------------


def _work_times(user, args: dict) -> dict:
    from app.services.timetracking import service
    from app.services.timetracking.calc import month_bounds, week_bounds
    from app.services.timetracking.clock import local_today, utcnow_naive

    period = args.get("zeitraum") if args.get("zeitraum") in ("heute", "woche", "monat") else "woche"
    now = utcnow_naive()
    today = local_today(now)
    start, end = (today, today) if period == "heute" else (week_bounds(today) if period == "woche" else month_bounds(today))
    summary = service.summarize_user_period(user, start, end, now=now)
    state = service.get_stamp_state(user.id, now)
    days = [
        {
            "datum": d.day.strftime("%d.%m.%Y"),
            "beginn": _fmt_dt(d.first_start),
            "ende": _fmt_dt(d.last_end) if not d.is_running else "läuft",
            "pausen": format_duration(d.break_seconds),
            "netto": format_duration(d.net_seconds),
            "soll": format_duration(d.target_seconds),
        }
        for d in summary.days
        if d.net_seconds or d.is_running or d.first_start
    ]
    return {
        "zeitraum": f"{start.strftime('%d.%m.%Y')} – {end.strftime('%d.%m.%Y')}",
        "status_jetzt": {"in": "eingestempelt", "break": "in Pause", "out": "ausgestempelt"}[state.status],
        "netto_gesamt": format_duration(summary.net_seconds),
        "soll_gesamt": format_duration(summary.target_seconds),
        "pausen_gesamt": format_duration(summary.break_seconds),
        "tage": days,
    }


def _open_entries(user, args: dict) -> dict:
    from app.services import leipziger_todo
    from app.utils.vermittlernummer import vermittlernummer_key

    documents = leipziger_todo.list_documents()
    if not documents:
        return {"hinweis": "Es ist noch keine Leipziger Liste importiert."}
    entries = leipziger_todo.load_entries(documents[0])
    visible = leipziger_todo.visible_entries(
        entries, vermittlernummer_key(user.vermittlernummer), all_brokers=bool(user.is_office_admin)
    )
    todo = leipziger_todo.search_entries([e for e in visible if e.is_todo], args.get("suche"))
    todo = leipziger_todo.tab_entries(todo, "zu-erledigen")
    return {
        "liste": documents[0].original_filename,
        "anzahl_zu_erledigen": len(todo),
        "vorgaenge": [
            {"vertrag": e.number, "art": e.label, "kunde": e.customer, "vermittler": e.broker_number, "status": e.status_code}
            for e in todo[:MAX_ROWS]
        ],
        "weitere": max(0, len(todo) - MAX_ROWS),
    }


def _search(user, args: dict) -> dict:
    from app.services.global_search import MIN_QUERY_LENGTH, global_search

    query = str(args.get("suchbegriff") or "")
    results = global_search(user, query)
    if len(results.query) < MIN_QUERY_LENGTH:
        return {"hinweis": f"Bitte mindestens {MIN_QUERY_LENGTH} Zeichen angeben."}
    return {
        "kunden": [
            {
                "name": c["name"],
                "kundennummer": c.get("customer_number"),
                "telefon": c.get("phone"),
                "ort": " ".join(filter(None, [c.get("postal_code"), c.get("city")])) or None,
                "vorgaenge": c.get("entries"),
                "memos": c.get("memos"),
            }
            for c in results.customers
        ],
        "weitere_kunden": results.customers_more,
        "vorgaenge": [
            {
                "vertrag": r.contract_number,
                "kunde": r.customer_name,
                "vermittler": r.broker_number,
                "status": r.status_code,
                "sparte": r.product_line,
                "beginn": r.start_date.strftime("%d.%m.%Y") if r.start_date else None,
            }
            for r in results.entries
        ],
        "weitere_vorgaenge": results.entries_more,
    }


def _tasks(user, args: dict) -> dict:
    from app.models import Task
    from app.models.enums import PRIORITY_ORDER, TaskStatus
    from app.services.timetracking.clock import local_to_utc_naive, local_today

    if args.get("status") == "heute_erledigt":
        since = local_to_utc_naive(local_today(), time(0, 0))
        tasks = Task.query.filter(Task.status == TaskStatus.DONE, Task.resolved_at >= since).all()
    else:
        tasks = Task.query.filter(Task.status == TaskStatus.OPEN).all()
        tasks.sort(key=lambda t: (-PRIORITY_ORDER[t.priority], t.due_date or date.max))
    return {
        "anzahl": len(tasks),
        "aufgaben": [
            {
                "titel": t.title,
                "kunde": t.customer.name if t.customer else None,
                "prioritaet": t.priority.value,
                "faellig": t.due_date.strftime("%d.%m.%Y") if t.due_date else None,
                "zustaendig": t.assigned_user.email if t.assigned_user else None,
            }
            for t in tasks[:MAX_ROWS]
        ],
        "weitere": max(0, len(tasks) - MAX_ROWS),
    }


def _team_presence(user, args: dict) -> dict:
    from app.models import User
    from app.services.leipziger_todo import user_display_name
    from app.services.timetracking import service
    from app.services.timetracking.clock import utcnow_naive
    from app.services.user_admin import office_members_query

    members = office_members_query().filter(User.is_active.is_(True)).all()
    rows = service.build_team_overview(members, utcnow_naive())
    labels = {"in": "anwesend", "break": "Pause", "out": "abwesend"}
    return {
        "mitarbeiter": [
            {"name": user_display_name(r.user), "status": labels.get(r.state, r.state), "heute_netto": format_duration(r.today.net_seconds)}
            for r in rows[:50]
        ]
    }


TOOLS: dict[str, Tool] = {
    t.name: t
    for t in (
        Tool(
            "meine_arbeitszeiten",
            "Arbeitszeiten des angemeldeten Benutzers aus der Zeiterfassung (Beginn, Ende, Pausen, Netto, Soll) "
            "sowie der aktuelle Stempelstatus.",
            {
                "type": "object",
                "properties": {"zeitraum": {"type": "string", "enum": ["heute", "woche", "monat"]}},
                "required": ["zeitraum"],
                "additionalProperties": False,
            },
            "timetracking.index",
            _work_times,
            label="Arbeitszeiten",
        ),
        Tool(
            "offene_vorgaenge",
            "Offene Vorgänge ('Zu erledigen') der aktuellen Leipziger Liste. Mitarbeiter sehen nur Vorgänge "
            "ihrer eigenen Vermittlernummer, Büro-Admins alle. Optional nach Vertragsnummer, Kunde oder "
            "Vermittler filtern.",
            {
                "type": "object",
                "properties": {"suche": {"type": "string", "description": "optionaler Suchbegriff"}},
                "additionalProperties": False,
            },
            "leipziger.index",
            _open_entries,
            label="Leipziger Liste",
        ),
        Tool(
            "suche",
            "Sucht Kunden (nur Büro-Admins) und Vorgänge der aktuellen Leipziger Liste nach Name, Telefon-, "
            "Kunden- oder Vertragsnummer.",
            {
                "type": "object",
                "properties": {"suchbegriff": {"type": "string"}},
                "required": ["suchbegriff"],
                "additionalProperties": False,
            },
            "search.search",
            _search,
            label="Suche",
        ),
        Tool(
            "aufgaben",
            "Aufgaben des Büros: offene Aufgaben (nach Priorität) oder heute erledigte Aufgaben.",
            {
                "type": "object",
                "properties": {"status": {"type": "string", "enum": ["offen", "heute_erledigt"]}},
                "required": ["status"],
                "additionalProperties": False,
            },
            "tasks.list_tasks",
            _tasks,
            office_admin_only=True,
            label="Aufgaben",
        ),
        Tool(
            "team_anwesenheit",
            "Aktueller Anwesenheitsstatus und heutige Arbeitszeit aller Mitarbeiter des Büros.",
            {"type": "object", "properties": {}, "additionalProperties": False},
            "timetracking.team",
            _team_presence,
            office_admin_only=True,
            label="Team-Anwesenheit",
        ),
    )
}


def is_allowed(user, tool: Tool) -> bool:
    if tool.endpoint not in current_app.view_functions:
        return False
    if tool.office_admin_only and not user.is_office_admin:
        return False
    return is_endpoint_allowed(user, tool.endpoint, tool.endpoint.split(".", 1)[0])


def available_for(user) -> list[Tool]:
    if not (user.is_office_admin or user.is_employee):
        # SUPER_ADMIN: kein Zugriff auf Buerodaten (bestehendes Rechtemodell).
        return []
    return [tool for tool in TOOLS.values() if is_allowed(user, tool)]


def execute(user, name: str, arguments: dict) -> str:
    """Fuehrt ein Werkzeug aus und liefert das Ergebnis als JSON-Text (fuer das Modell)."""
    tool = TOOLS.get(name)
    if tool is None or not (user.is_office_admin or user.is_employee) or not is_allowed(user, tool):
        return json.dumps({"fehler": "Dieses Werkzeug ist für den Benutzer nicht verfügbar."}, ensure_ascii=False)
    try:
        result = tool.handler(user, arguments if isinstance(arguments, dict) else {})
    except Exception:  # noqa: BLE001 - Fehler eines Werkzeugs darf die Antwort nicht abbrechen.
        current_app.logger.exception("zentriq_ai.tool_failed tool=%s", name)
        return json.dumps({"fehler": "Das Werkzeug ist gerade nicht verfügbar."}, ensure_ascii=False)
    text = json.dumps(result, ensure_ascii=False, default=str)
    return text if len(text) <= MAX_RESULT_CHARS else text[:MAX_RESULT_CHARS] + ' …"(gekürzt)"'
