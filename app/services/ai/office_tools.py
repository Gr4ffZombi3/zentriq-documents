"""Buero-Werkzeuge des Assistenten: Kundensuche, Kundenanlage, Buero-Profil, freie Termine,
Terminbuchung. Alle Entscheidungen (Suche, Validierung, Kalenderberechnung, Speichern) trifft das
Backend; Rechte wie in der Oberflaeche (Endpunkt-Bindung in tools.Tool). Schreibende Werkzeuge
bereiten nur vor - ausgefuehrt wird erst nach Bestaetigung im Chat (actions.confirm)."""

from __future__ import annotations

from datetime import date, time

from app.services.ai.tools import Tool


def _customer_row(c) -> dict:
    return {"kunde_id": c.id, "name": c.name, "telefon": c.phone, "email": c.email, "ort": " ".join(filter(None, [c.postal_code, c.city])) or None}


def _search_customers(user, args: dict) -> dict:
    from sqlalchemy import func

    from app.models import Customer
    from app.services.customer_notes import (
        _by_name,
        _load_customers,
        customers_by_vn,
        search_by_name_fragment,
    )
    from app.utils.customer_keys import normalize_phone, vn_key

    term = " ".join(str(args.get("suchbegriff") or "").split())[:120]
    if len(term) < 2:
        return {"fehler": "Bitte mindestens 2 Zeichen angeben."}
    found = []
    if (key := vn_key(term)) is not None:
        found = _load_customers(customers_by_vn(key))
    elif "@" in term:
        found = Customer.query.filter(func.lower(Customer.email) == term.lower()).limit(10).all()
    elif (phone := normalize_phone(term)) is not None:
        found = Customer.query.filter(Customer.phone_key == phone).limit(10).all()
    else:
        found = _by_name(term) or search_by_name_fragment(term)
    return {"treffer": [_customer_row(c) for c in found[:10]], "anzahl": len(found)}


def _office_profile(user, args: dict) -> dict:
    from app.services import availability

    s = availability.settings()
    hours = availability.office_hours(s)
    return {
        "buero": {k: getattr(s, k) for k in ("office_name", "email", "phone", "address", "website")} if s else None,
        "signatur_hinterlegt": bool(s and s.signature),
        "buerozeiten": {availability.WEEKDAYS[d - 1]: f"{a.strftime('%H:%M')}–{b.strftime('%H:%M')}" for d, (a, b) in sorted(hours.items())},
        "terminplanung_eingerichtet": bool(hours),
        "standard_dauer_minuten": s.default_minutes if s else None,
        "puffer_minuten": s.buffer_minutes if s else None,
    }


def _member(name: str | None, user):
    from app.services import calendar as cal
    from app.services.calendar_intent import _members_matching

    members = cal.office_members()
    if not name:
        return next((m for m in members if m.id == user.id), None), None
    found = _members_matching(name, members)
    if len(found) != 1:
        return None, ("Mehrere Mitarbeiter passen." if found else f"„{name}“ ist kein Mitarbeiter des Büros.")
    return found[0], None


def _free_slots(user, args: dict) -> dict:
    from app.services import availability
    from app.services.calendar import member_name
    from app.services.timetracking.clock import local_today

    assignee, problem = _member(args.get("mitarbeiter"), user)
    if assignee is None:
        return {"fehler": problem or "Kein zuständiger Mitarbeiter."}
    s = availability.settings()
    if not availability.planning_ready(s):
        return {"fehler": "Bürozeiten für die Terminplanung sind nicht hinterlegt (Kalender › Bürozeiten & Büro-Profil)."}
    minutes = int(args.get("dauer_minuten") or s.default_minutes or 30)
    if not 10 <= minutes <= 240:
        return {"fehler": "Ungültige Dauer."}
    prefs = availability.preferences_from(str(args.get("wunsch") or ""), local_today())
    slots, honored = availability.free_slots(assignee, minutes, prefs, count=int(args.get("anzahl") or 3), s=s)
    return {"mitarbeiter": member_name(assignee), "dauer_minuten": minutes, "wunsch_beruecksichtigt": honored,
            "freie_termine": [{"text": x.label(), "start": x.iso()} for x in slots]}


def _prepare_customer(user, args: dict) -> dict:
    from app.services import customer_create

    parts = [args.get("vorname"), args.get("nachname")]
    if not all(isinstance(v, str) and v.strip() for v in parts):
        return {"fehler": "Vor- und Nachname sind erforderlich."}
    clean = {k: str(v).strip() for k, v in args.items() if isinstance(v, (str, int)) and str(v).strip()}
    first, last = customer_create._clean_name_part(clean["vorname"]), customer_create._clean_name_part(clean["nachname"])
    if not first or not last:
        return {"fehler": "Der Name enthält ungültige Zeichen."}
    summary = f"Kunde anlegen: {first} {last}" + "".join(f", {clean[k]}" for k in ("telefon", "email", "plz") if k in clean)
    return {"aktion": {"tool": "kunde_anlegen", "arguments": clean, "summary": summary}}


def _prepare_booking(user, args: dict) -> dict:
    from app.models import Customer
    from app.services.calendar import member_name

    try:
        day = date.fromisoformat(str(args.get("datum")))
        hh, mm = (int(x) for x in str(args.get("uhrzeit")).split(":"))
        start = time(hh, mm)
    except (TypeError, ValueError):
        return {"fehler": "Datum (JJJJ-MM-TT) und Uhrzeit (HH:MM) sind erforderlich."}
    assignee, problem = _member(args.get("mitarbeiter"), user)
    if assignee is None:
        return {"fehler": problem or "Kein zuständiger Mitarbeiter."}
    customer = None
    if args.get("kunde_id"):
        customer = Customer.query.filter(Customer.id == int(args["kunde_id"])).first()
        if customer is None:
            return {"fehler": "Der Kunde wurde nicht gefunden."}
    minutes = int(args.get("dauer_minuten") or 30)
    arguments = {"datum": day.isoformat(), "uhrzeit": start.strftime("%H:%M"), "dauer_minuten": minutes, "mitarbeiter_id": assignee.id,
                 "kunde_id": customer.id if customer else None, "titel": str(args.get("titel") or "")[:120]}
    summary = f"Termin buchen: {day.strftime('%d.%m.%Y')} {start.strftime('%H:%M')} Uhr, {minutes} Min., {member_name(assignee)}" + (f", Kunde {customer.name}" if customer else "")
    return {"aktion": {"tool": "termin_buchen", "arguments": arguments, "summary": summary}}


# --- Ausfuehrung nach Bestaetigung (actions.EXECUTORS) ---------------------------------------


def execute_customer(user, arguments: dict) -> str:
    from app.services import customer_create

    text_parts = ["neuer kunde", arguments.get("vorname", ""), arguments.get("nachname", ""), arguments.get("telefon", ""),
                  arguments.get("strasse", ""), arguments.get("hausnummer", ""), arguments.get("plz", ""), arguments.get("ort", ""),
                  arguments.get("email", "")]
    result = customer_create.handle(user, " ".join(p for p in text_parts if p), first_name=arguments.get("vorname"),
                                    last_name=arguments.get("nachname"))
    if result.get("kind") == "card":
        return result.get("message") if result.get("created") else (result.get("notice") or "Der Kunde ist bereits angelegt.")
    return result.get("message") or "Der Kunde wurde nicht angelegt – bitte im Eingabefeld prüfen."


def execute_booking(user, arguments: dict) -> str:
    from app.models import Customer
    from app.services import booking
    from app.services import calendar as cal
    from app.services.ai.actions import ActionError
    from app.services.timetracking.clock import local_to_utc_naive

    members = {m.id: m for m in cal.office_members()}
    assignee = members.get(int(arguments.get("mitarbeiter_id") or 0))
    if assignee is None:
        raise ActionError("Der Mitarbeiter gehört nicht (mehr) zum Büro.", 409)
    customer = Customer.query.filter(Customer.id == int(arguments["kunde_id"])).first() if arguments.get("kunde_id") else None
    day = date.fromisoformat(arguments["datum"])
    hh, mm = (int(x) for x in arguments["uhrzeit"].split(":"))
    start_utc = local_to_utc_naive(day, time(hh, mm))
    title = arguments.get("titel") or (f"Termin {customer.name}" if customer else "Termin")
    result = booking.book(user, assignee=assignee, start_utc=start_utc, minutes=int(arguments.get("dauer_minuten") or 30),
                          customer=customer, title=title)
    if result.reason:
        alt = (" Freie Alternativen: " + "; ".join(a.label() for a in result.alternatives) + ".") if result.alternatives else ""
        raise ActionError(f"Nicht gebucht: {result.reason}{alt}", 409)
    when = cal.local_iso(result.event.starts_at).replace("T", " ")
    if not result.created:
        return "Dieser Termin war bereits gebucht."
    return f"Termin gebucht (bestätigt): {when} Uhr, {cal.member_name(assignee)}" + (f", Kunde {customer.name}" if customer else "") + "."



TOOLS = (
    Tool("kunden_suchen", "Sucht Kunden des eigenen Büros nach Name, VN-Nummer, E-Mail oder Telefonnummer.",
         {"type": "object", "properties": {"suchbegriff": {"type": "string"}}, "required": ["suchbegriff"], "additionalProperties": False},
         "zentriq.api_customer_card", _search_customers, label="Kundensuche", progress="Suche Kunden"),
    Tool("bueroprofil", "Büro-Profil (Name, E-Mail, Telefon, Anschrift für Absender/Signatur) und Regeln der Terminplanung.",
         {"type": "object", "properties": {}, "additionalProperties": False},
         "calendar.index", _office_profile, label="Büro-Profil", progress="Lese Büro-Profil"),
    Tool("freie_termine", "Ermittelt tatsächlich freie Termine im Bürokalender (Bürozeiten, Termine, Puffer). Wunsch als Text, z. B. 'nachmittags, Dienstag'.",
         {"type": "object", "properties": {"mitarbeiter": {"type": "string"}, "dauer_minuten": {"type": "integer", "minimum": 10, "maximum": 240},
                                           "wunsch": {"type": "string"}, "anzahl": {"type": "integer", "minimum": 1, "maximum": 5}},
          "additionalProperties": False},
         "calendar.index", _free_slots, label="Freie Termine", progress="Prüfe Kalender"),
    Tool("kunde_anlegen", "Bereitet die Anlage eines Kunden vor (nur belegte Angaben). Wird erst nach Bestätigung ausgeführt.",
         {"type": "object", "properties": {k: {"type": "string"} for k in ("vorname", "nachname", "telefon", "email", "strasse", "hausnummer", "plz", "ort")},
          "required": ["vorname", "nachname"], "additionalProperties": False},
         "zentriq.api_customer_input", _prepare_customer, label="Kunde anlegen", progress="Bereite Aktion vor", write=True),
    Tool("termin_buchen", "Bereitet eine Terminbuchung vor (nur bei ausdrücklichem Auftrag). Verfügbarkeit wird vor dem Speichern erneut geprüft.",
         {"type": "object", "properties": {"datum": {"type": "string", "description": "JJJJ-MM-TT"}, "uhrzeit": {"type": "string", "description": "HH:MM"},
                                           "dauer_minuten": {"type": "integer", "minimum": 10, "maximum": 240}, "mitarbeiter": {"type": "string"},
                                           "kunde_id": {"type": "integer"}, "titel": {"type": "string"}},
          "required": ["datum", "uhrzeit"], "additionalProperties": False},
         "calendar.index", _prepare_booking, label="Termin buchen", progress="Bereite Aktion vor", write=True),
)
