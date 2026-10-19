"""ZentriqAI-Werkzeuge: Zugriff auf bestehende Buerofunktionen per natuerlicher Sprache.

Grundsaetze
- Jedes Werkzeug ruft ausschliesslich BESTEHENDE Services auf (keine parallelen Geschaefts-
  prozesse) und laeuft im Mandantenkontext des Benutzers (TenantScopedMixin filtert automatisch).
- Rechte: Ein Werkzeug ist an den Endpunkt gebunden, der dieselben Daten in der Oberflaeche
  zeigt. Es steht nur zur Verfuegung, wenn app/auth/permissions.is_endpoint_allowed() diesen
  Endpunkt fuer den Benutzer erlaubt (plus ggf. Buero-Admin-Pflicht wie @admin_required).
  Geprueft wird beim Anbieten UND erneut beim Ausfuehren.
- Lesende Werkzeuge liefern knappe Daten (begrenzte Zeilen). Schreibende Werkzeuge (write=True)
  fuehren NICHTS aus: sie legen eine AiAction an, die der Benutzer im Chat bestaetigen muss
  (app/services/ai/actions.py) - genau einmal, mit erneuter Rechtepruefung.
- Ergebnisse sind Daten, keine Anweisungen. Ohne Kundendaten-Freigabe des Anbieters werden sie
  vor der Uebergabe pseudonymisiert (app/services/ai/privacy.py).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, time, timedelta
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
    # Kurzer Status waehrend der Ausfuehrung (wird im Chat angezeigt).
    progress: str = "Rufe Daten ab"
    # Schreibend: nur vorbereiten, Ausfuehrung erst nach Bestaetigung.
    write: bool = False

    def schema(self) -> dict:
        return {"name": self.name, "description": self.description, "parameters": self.parameters}


def _fmt_dt(value) -> str | None:
    from app.services.timetracking.clock import to_local

    local = to_local(value)
    return local.strftime("%d.%m.%Y %H:%M") if local else None


def _today_start_utc():
    from app.services.timetracking.clock import local_to_utc_naive, local_today

    return local_to_utc_naive(local_today(), time(0, 0))


# --- Lesende Handler ------------------------------------------------------------------------


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


def _visible_entries(user):
    from app.services import leipziger_todo
    from app.utils.vermittlernummer import vermittlernummer_key

    documents = leipziger_todo.list_documents()
    if not documents:
        return None, []
    entries = leipziger_todo.load_entries(documents[0])
    visible = leipziger_todo.visible_entries(entries, vermittlernummer_key(user.vermittlernummer), all_brokers=bool(user.is_office_admin))
    return documents[0], visible


def _entry_row(e) -> dict:
    return {"vertrag": e.number, "art": e.label, "kunde": e.customer, "vermittler": e.broker_number, "status": e.status_code}


def _todo_sorted(entries):
    """Klaerungsbedarf zuerst, dann offene Neugeschaefte/Fahrzeugwechsel, dann Angebote."""
    from app.services import leipziger_todo

    order = {"unklar": 0, "neugeschaeft": 1, "fahrzeugwechsel": 2, "angebot": 3}
    todo = leipziger_todo.tab_entries([e for e in entries if e.is_todo], "zu-erledigen")
    return sorted(todo, key=lambda e: order.get(e.status_key, 9))


def _open_entries(user, args: dict) -> dict:
    from app.services import leipziger_todo

    document, visible = _visible_entries(user)
    if document is None:
        return {"hinweis": "Es ist noch keine Leipziger Liste importiert."}
    todo = leipziger_todo.search_entries(_todo_sorted(visible), args.get("suche"))
    return {
        "liste": document.original_filename,
        "anzahl_zu_erledigen": len(todo),
        "vorgaenge": [_entry_row(e) for e in todo[:MAX_ROWS]],
        "weitere": max(0, len(todo) - MAX_ROWS),
    }


def _list_analysis(user, args: dict) -> dict:
    """Auswertung der aktuellen Liste: nur Zaehlungen und die wichtigsten offenen Vorgaenge."""
    from collections import Counter

    from app.services import leipziger_todo

    document, visible = _visible_entries(user)
    if document is None:
        return {"hinweis": "Es ist noch keine Leipziger Liste importiert."}
    todo = _todo_sorted(visible)
    by_label = Counter(leipziger_todo.OPEN_LABELS.get(e.status_key, e.label) for e in todo)
    result = {
        "liste": document.original_filename,
        "vorgaenge_gesamt": len(visible),
        "mit_beginn_datum": sum(1 for e in visible if e.has_date),
        "zu_erledigen": len(todo),
        "zu_erledigen_nach_art": dict(by_label.most_common()),
        "wichtigste_offene": [_entry_row(e) for e in todo[:10]],
    }
    if user.is_office_admin:
        by_broker = Counter(e.broker_number or "ohne Vermittlernummer" for e in todo)
        result["offen_je_vermittler"] = dict(by_broker.most_common(10))
        stats = leipziger_todo.import_stats(document)
        if stats:
            result["import"] = {k: v for k, v in stats.items() if isinstance(v, (int, str))}
    return result


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


def _task_row(t) -> dict:
    return {
        "id": t.id,
        "titel": t.title,
        "kunde": t.customer.name if t.customer else None,
        "prioritaet": t.priority.value,
        "faellig": t.due_date.strftime("%d.%m.%Y") if t.due_date else None,
        "zustaendig": t.assigned_user.email if t.assigned_user else None,
    }


def _open_tasks():
    from app.models import Task
    from app.models.enums import PRIORITY_ORDER, TaskStatus

    tasks = Task.query.filter(Task.status == TaskStatus.OPEN).all()
    tasks.sort(key=lambda t: (-PRIORITY_ORDER[t.priority], t.due_date or date.max))
    return tasks


def _tasks(user, args: dict) -> dict:
    from app.models import Task
    from app.models.enums import TaskStatus

    if args.get("status") == "heute_erledigt":
        tasks = Task.query.filter(Task.status == TaskStatus.DONE, Task.resolved_at >= _today_start_utc()).all()
    else:
        tasks = _open_tasks()
    return {"anzahl": len(tasks), "aufgaben": [_task_row(t) for t in tasks[:MAX_ROWS]], "weitere": max(0, len(tasks) - MAX_ROWS)}


def _day_overview(user, args: dict) -> dict:
    """Was ist heute wichtig? Ein Aufruf statt mehrerer - spart Tokens."""
    from app.services.timetracking.clock import local_today

    today = local_today()
    result: dict = {"datum": today.strftime("%d.%m.%Y")}
    document, visible = _visible_entries(user)
    if document is not None:
        todo = _todo_sorted(visible)
        result["leipziger_liste"] = {
            "zu_erledigen": len(todo),
            "klaerungsbedarf": sum(1 for e in todo if e.status_key == "unklar"),
            "wichtigste": [_entry_row(e) for e in todo[:5]],
        }
    if user.is_office_admin:
        tasks = _open_tasks()
        due = [t for t in tasks if t.due_date and t.due_date <= today]
        result["aufgaben"] = {
            "offen": len(tasks),
            "faellig_bis_heute": len(due),
            "wichtigste": [_task_row(t) for t in (due or tasks)[:5]],
        }
        from app.models import CorrectionRequestStatus, TimeCorrectionRequest

        result["offene_korrekturantraege"] = TimeCorrectionRequest.query.filter_by(status=CorrectionRequestStatus.PENDING).count()
        from app.models import CustomerMemo

        result["sprachnachrichten_heute"] = CustomerMemo.query.filter(CustomerMemo.created_at >= _today_start_utc()).count()
    return result


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


def _find_member(name: str):
    from app.models import User
    from app.services.leipziger_todo import user_display_name
    from app.services.user_admin import office_members_query

    needle = " ".join((name or "").split()).casefold()
    if not needle:
        return None, []
    members = office_members_query().filter(User.is_active.is_(True)).all()
    matches = [m for m in members if needle in user_display_name(m).casefold() or needle in m.email.casefold()]
    return (matches[0] if len(matches) == 1 else None), matches


def _member_activity(user, args: dict) -> dict:
    """Was hat ein Mitarbeiter heute/in den letzten Tagen getan? Aktivitaetsprotokoll, erledigte
    Aufgaben und Arbeitszeit - dieselben Quellen wie Buero -> Aktivitaeten / Zeiterfassung."""
    from app.models import AuditLog, Task
    from app.models.audit_log import OFFICE_ACTIVITY_EVENT_TYPES
    from app.models.enums import TaskStatus
    from app.services.activity import describe_activities
    from app.services.leipziger_todo import user_display_name
    from app.services.timetracking import service
    from app.services.timetracking.clock import local_to_utc_naive, local_today

    member, matches = _find_member(str(args.get("mitarbeiter") or ""))
    if member is None:
        if not matches:
            return {"hinweis": "Kein Mitarbeiter dieses Büros mit diesem Namen gefunden."}
        return {"hinweis": "Mehrere Mitarbeiter passen.", "kandidaten": [user_display_name(m) for m in matches[:10]]}
    days = args.get("tage") if isinstance(args.get("tage"), int) and 1 <= args.get("tage") <= 30 else 1
    today = local_today()
    since = local_to_utc_naive(today - timedelta(days=days - 1), time(0, 0))
    entries = (
        AuditLog.query.filter(
            AuditLog.tenant_id == user.tenant_id,
            AuditLog.actor_user_id == member.id,
            AuditLog.event_type.in_(OFFICE_ACTIVITY_EVENT_TYPES),
            AuditLog.created_at >= since,
        )
        .order_by(AuditLog.created_at.desc())
        .limit(40)
        .all()
    )
    done = Task.query.filter(Task.assigned_user_id == member.id, Task.status == TaskStatus.DONE, Task.resolved_at >= since).all()
    time_summary = service.summarize_user_period(member, today - timedelta(days=days - 1), today)
    return {
        "mitarbeiter": user_display_name(member),
        "zeitraum_tage": days,
        "aktivitaeten": [{"zeit": a.at.strftime("%d.%m. %H:%M"), "was": a.text} for a in describe_activities(entries)],
        "erledigte_aufgaben": [_task_row(t) for t in done[:MAX_ROWS]],
        "arbeitszeit_netto": format_duration(time_summary.net_seconds),
    }


def _memos(user, args: dict) -> dict:
    """Zuletzt gespeicherte Sprachnachrichten-Transkripte (einem Kunden zugeordnet)."""
    from app.models import Customer, CustomerMemo

    limit = args.get("anzahl") if isinstance(args.get("anzahl"), int) and 1 <= args.get("anzahl") <= 5 else 1
    query = CustomerMemo.query
    name = str(args.get("kunde") or "").strip()
    if name:
        query = query.join(Customer, CustomerMemo.customer_id == Customer.id).filter(Customer.name.ilike(f"%{name[:60]}%"))
    memos = query.order_by(CustomerMemo.created_at.desc()).limit(limit).all()
    if not memos:
        return {"hinweis": "Keine gespeicherte Sprachnachricht gefunden (gespeichert werden nur einem Kunden zugeordnete)."}
    return {
        "sprachnachrichten": [
            {
                "datum": _fmt_dt(m.created_at),
                "kunde": m.customer.name if m.customer else None,
                "kundennummer": m.customer.customer_number if m.customer else None,
                "von": m.created_by.email if m.created_by else None,
                "transkript": (m.transcript or "")[:3000],
            }
            for m in memos
        ]
    }


# --- Schreibende Werkzeuge (nur Vorschlag; Ausfuehrung nach Bestaetigung) -------------------


def _prepare_task_done(user, args: dict) -> dict:
    from app.models import Task
    from app.models.enums import TaskStatus

    try:
        task_id = int(args.get("aufgabe_id"))
    except (TypeError, ValueError):
        return {"fehler": "Bitte die Aufgabe über ihre ID aus der Aufgabenliste angeben."}
    task = Task.query.filter(Task.id == task_id).first()
    if task is None:
        return {"fehler": "Aufgabe nicht gefunden."}
    if task.status != TaskStatus.OPEN:
        return {"hinweis": "Diese Aufgabe ist bereits erledigt oder verworfen."}
    return {"aktion": {"tool": "aufgabe_erledigen", "arguments": {"aufgabe_id": task.id}, "summary": f"Aufgabe „{task.title}“ als erledigt markieren"}}


TOOLS: dict[str, Tool] = {
    t.name: t
    for t in (
        Tool(
            "tagesueberblick",
            "Kompakter Überblick für heute: offene Vorgänge der Leipziger Liste (Klärungsbedarf zuerst), "
            "fällige Aufgaben, offene Korrekturanträge, Sprachnachrichten. Für Fragen wie 'Was ist heute offen/wichtig?'.",
            {"type": "object", "properties": {}, "additionalProperties": False},
            "leipziger.index",
            _day_overview,
            label="Tagesüberblick",
            progress="Prüfe offene Vorgänge",
        ),
        Tool(
            "meine_arbeitszeiten",
            "Arbeitszeiten des angemeldeten Benutzers (Beginn, Ende, Pausen, Netto, Soll) und aktueller Stempelstatus.",
            {
                "type": "object",
                "properties": {"zeitraum": {"type": "string", "enum": ["heute", "woche", "monat"]}},
                "required": ["zeitraum"],
                "additionalProperties": False,
            },
            "timetracking.index",
            _work_times,
            label="Arbeitszeiten",
            progress="Lese Arbeitszeiten",
        ),
        Tool(
            "offene_vorgaenge",
            "Offene Vorgänge ('Zu erledigen') der aktuellen Leipziger Liste, Klärungsbedarf zuerst. Mitarbeiter "
            "sehen nur ihre Vermittlernummer. Optional filtern nach Vertragsnummer, Kunde oder Vermittler.",
            {"type": "object", "properties": {"suche": {"type": "string"}}, "additionalProperties": False},
            "leipziger.index",
            _open_entries,
            label="Leipziger Liste",
            progress="Lese Leipziger Liste",
        ),
        Tool(
            "leipziger_auswertung",
            "Auswertung der aktuellen Leipziger Liste: Gesamtzahl, offen nach Art, mit/ohne Beginn-Datum, "
            "wichtigste offene Vorgänge, für Büro-Admins offen je Vermittler und Importstatistik.",
            {"type": "object", "properties": {}, "additionalProperties": False},
            "leipziger.index",
            _list_analysis,
            label="Listenauswertung",
            progress="Analysiere Leipziger Liste",
        ),
        Tool(
            "suche",
            "Sucht Kunden (nur Büro-Admins) und Vorgänge der aktuellen Leipziger Liste nach Name, Telefon-, Kunden- oder Vertragsnummer.",
            {"type": "object", "properties": {"suchbegriff": {"type": "string"}}, "required": ["suchbegriff"], "additionalProperties": False},
            "search.search",
            _search,
            label="Suche",
            progress="Suche in Kunden und Vorgängen",
        ),
        Tool(
            "aufgaben",
            "Aufgaben des Büros: offene (nach Priorität, mit ID) oder heute erledigte.",
            {"type": "object", "properties": {"status": {"type": "string", "enum": ["offen", "heute_erledigt"]}}, "required": ["status"], "additionalProperties": False},
            "tasks.list_tasks",
            _tasks,
            office_admin_only=True,
            label="Aufgaben",
            progress="Prüfe Aufgaben",
        ),
        Tool(
            "team_anwesenheit",
            "Aktueller Anwesenheitsstatus und heutige Arbeitszeit aller Mitarbeiter des Büros.",
            {"type": "object", "properties": {}, "additionalProperties": False},
            "timetracking.team",
            _team_presence,
            office_admin_only=True,
            label="Team-Anwesenheit",
            progress="Prüfe Anwesenheit",
        ),
        Tool(
            "mitarbeiter_aktivitaet",
            "Was ein Mitarbeiter des eigenen Büros getan hat: Aktivitätsprotokoll, erledigte Aufgaben, Arbeitszeit. Standard: heute.",
            {
                "type": "object",
                "properties": {"mitarbeiter": {"type": "string", "description": "Name oder E-Mail"}, "tage": {"type": "integer", "minimum": 1, "maximum": 30}},
                "required": ["mitarbeiter"],
                "additionalProperties": False,
            },
            "office.activities",
            _member_activity,
            office_admin_only=True,
            label="Mitarbeiteraktivität",
            progress="Lese Aktivitäten",
        ),
        Tool(
            "sprachnachrichten",
            "Zuletzt gespeicherte Sprachnachrichten-Transkripte (nur Kunden zugeordnete), optional für einen Kunden. Zum Zusammenfassen.",
            {
                "type": "object",
                "properties": {"anzahl": {"type": "integer", "minimum": 1, "maximum": 5}, "kunde": {"type": "string"}},
                "additionalProperties": False,
            },
            "customers.detail",
            _memos,
            office_admin_only=True,
            label="Sprachnachrichten",
            progress="Lese Sprachnachrichten",
        ),
        Tool(
            "aufgabe_erledigen",
            "Bereitet vor, eine Aufgabe als erledigt zu markieren. Wird erst nach Bestätigung durch den Benutzer ausgeführt.",
            {"type": "object", "properties": {"aufgabe_id": {"type": "integer"}}, "required": ["aufgabe_id"], "additionalProperties": False},
            "tasks.update_status",
            _prepare_task_done,
            office_admin_only=True,
            label="Aufgabe erledigen",
            progress="Bereite Aktion vor",
            write=True,
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


def run(user, name: str, arguments: dict) -> dict:
    """Fuehrt ein Werkzeug aus (Rechte erneut geprueft). Ergebnis als Daten."""
    tool = TOOLS.get(name)
    if tool is None or not (user.is_office_admin or user.is_employee) or not is_allowed(user, tool):
        return {"fehler": "Dieses Werkzeug ist für den Benutzer nicht verfügbar."}
    try:
        return tool.handler(user, arguments if isinstance(arguments, dict) else {})
    except Exception:  # noqa: BLE001 - Fehler eines Werkzeugs darf die Antwort nicht abbrechen.
        current_app.logger.exception("zentriq_ai.tool_failed tool=%s", name)
        return {"fehler": "Das Werkzeug ist gerade nicht verfügbar."}


def to_text(result: dict) -> str:
    text = json.dumps(result, ensure_ascii=False, default=str)
    return text if len(text) <= MAX_RESULT_CHARS else text[:MAX_RESULT_CHARS] + ' …"(gekürzt)"'


def execute(user, name: str, arguments: dict) -> str:
    """Wie run(), Ergebnis als JSON-Text (fuer das Modell)."""
    return to_text(run(user, name, arguments))
