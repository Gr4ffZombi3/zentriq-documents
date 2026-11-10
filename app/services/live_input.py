"""Live-Vorschau im Hauptchat (waehrend des Tippens, vor dem Absenden).

Drei Ablaeufe, alle lokal erkannt (0 Modellaufrufe) und rein LESEND:
- "Mueller"                                -> passende Kunden
- "Notiz Mueller: Fahrzeugschein fehlt"   -> zugeordnete Kundenakte + Notiztext
- "Termin Mueller Donnerstag 11:30 bei Laura" -> Kunde, Datum, Uhrzeit, Mitarbeiter, Dauer,
  Verfuegbarkeit (Buerozeiten + Kalender), bei Belegung freie Alternativen

Gespeichert wird hier nie. Das Speichern laeuft ueber die bestehenden Wege
(/zentriq/api/kunden/eingabe -> calendar_intent.handle mit "book" bzw. customer_notes), die
Rechte und Verfuegbarkeit beim Speichern erneut pruefen. Alles andere -> kind None (die Eingabe
geht beim Absenden wie bisher an den Chat)."""

import re
from datetime import date, datetime, time, timedelta

from app.models import Customer
from app.services import calendar as cal
from app.services import calendar_intent as ci
from app.services.timetracking.clock import local_to_utc_naive, local_today, utcnow_naive

MAX_CUSTOMERS = 6
DAY_SLOTS = 12
_NOTE = re.compile(r"^\s*notiz\s+(?:zu\s+|für\s+|fuer\s+)?([^:]{2,80}?)\s*:\s*(.*)$", re.I | re.S)
_TERM = re.compile(r"^\s*(?:bitte\s+)?(?:(?:einen?|neuen?)\s+)?termin\b(?!e)", re.I)
_NAME_ONLY = re.compile(r"^[A-Za-zÄÖÜäöüß][A-Za-zÄÖÜäöüß'-]+(?:\s+[A-Za-zÄÖÜäöüß][A-Za-zÄÖÜäöüß'-]+){0,2}$")
_WEEKDAY_LONG = ("Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag")
# Einzelwoerter, die keine Kundensuche ausloesen (Gruesse, Chat-Anfaenge).
_NOT_NAMES = {
    "hallo", "hi", "hey", "moin", "danke", "bitte", "ja", "nein", "ok", "okay", "test", "wie", "was", "wer", "wann", "warum",
    "welche", "welcher", "kannst", "schreib", "schreibe", "erstelle", "termin", "termine", "notiz", "kunde", "kunden",
    "guten", "morgen", "tag", "abend", "hilfe", "zeig", "zeige", "suche", "finde", "liste",
}


def classify(text: str) -> str | None:
    t = " ".join((text or "").split())
    if not t or len(t) > 300 or t.endswith("?"):
        return None
    if _NOTE.match(t):
        return "note"
    if _TERM.match(t) or (ci.is_booking_command(t) and not ci.is_availability_question(t)):
        return "appointment"
    if _NAME_ONLY.match(t) and len(t) >= 3 and t.split()[0].lower() not in _NOT_NAMES:
        return "customer"
    return None


def _customer_payload(customer: Customer) -> dict:
    from app.services.customer_notes import vn_numbers

    detail = " · ".join(v for v in (", ".join(vn_numbers(customer)) or None, customer.city) if v)
    return {"id": customer.id, "name": customer.name, "detail": detail}


def _find_customers(name: str) -> list[Customer]:
    from app.services.customer_notes import _by_name, search_by_name_fragment

    found = _by_name(name) or search_by_name_fragment(name)
    return found[: MAX_CUSTOMERS + 1]


def preview(user, text: str, overrides: dict | None = None, want_slots: bool = False) -> dict:
    overrides = overrides or {}
    t = " ".join((text or "").split())
    kind = classify(t)
    if kind == "customer":
        hits = _find_customers(t)
        if not hits:
            return {"kind": None}
        return {"kind": "customer", "customers": [_customer_payload(c) for c in hits[:MAX_CUSTOMERS]], "more": len(hits) > MAX_CUSTOMERS}
    if kind == "note":
        return _note_preview(t, overrides)
    if kind == "appointment":
        return _appointment_preview(user, t, overrides, want_slots)
    return {"kind": None}


# --- Notiz -----------------------------------------------------------------------------------


def _note_preview(text: str, overrides: dict) -> dict:
    match = _NOTE.match(text)
    name, body = match.group(1).strip(), match.group(2).strip()
    customer, options = None, []
    if str(overrides.get("customer_id") or "").isdigit():
        customer = Customer.query.filter(Customer.id == int(overrides["customer_id"])).first()
    if customer is None:
        found = _find_customers(name)
        if len(found) == 1:
            customer = found[0]
        options = [_customer_payload(c) for c in found[:MAX_CUSTOMERS]]
    status = "ok" if customer else ("ambiguous" if options else "missing")
    return {
        "kind": "note",
        "name": name,
        "customer": {"status": status, "value": _customer_payload(customer) if customer else None, "options": options if not customer else []},
        "text": {"status": "ok" if body else "missing", "value": body},
        "ready": bool(customer and body),
    }


# --- Termin ----------------------------------------------------------------------------------


def _member_payload(member) -> dict:
    return {"id": member.id, "name": cal.member_name(member)}


def _day_label(day: date, today: date) -> str:
    label = f"{_WEEKDAY_LONG[day.weekday()]}, {day.strftime('%d.%m.')}"
    if day == today:
        return "Heute, " + day.strftime("%d.%m.")
    if day == today + timedelta(days=1):
        return "Morgen, " + day.strftime("%d.%m.")
    return label


def _availability(assignee, start_utc: datetime, minutes: int) -> dict:
    """Frei/belegt laut Buerozeiten, Arbeitstagen und Kalender; bei Belegung Alternativen."""
    from app.services import availability, booking

    s = availability.settings()
    if availability.planning_ready(s):
        reason = availability.check(assignee, start_utc, minutes, s)
        alternatives = booking.alternatives_near(assignee, start_utc, minutes, s) if reason else []
        hours_known = True
    else:
        busy = cal.conflicts_for(assignee.id, start_utc, start_utc + timedelta(minutes=minutes))
        reason = f"{cal.member_name(assignee)} hat dann bereits „{busy[0].title}“." if busy else None
        if reason is None and start_utc < utcnow_naive():
            reason = "Der Zeitpunkt liegt in der Vergangenheit."
        alternatives = booking._calendar_gaps(assignee, start_utc, minutes) if reason else []
        hours_known = False
    return {
        "status": "busy" if reason else "free",
        "reason": reason,
        "alternatives": [{"start": a.iso(), "label": a.label()} for a in alternatives],
        "hours_known": hours_known,
    }


def day_slots(assignee, day: date, minutes: int) -> list[dict]:
    """Freie Anfangszeiten an einem Tag (Raster wie die Terminsuche), fuer die Uhrzeit-Auswahl."""
    from app.services import availability

    s = availability.settings()
    hours = availability.office_hours(s)
    step = availability.STEP_MINUTES
    window = hours.get(day.isoweekday()) if hours else (time(8, 0), time(18, 0))
    if window is None:
        return []
    now = utcnow_naive()
    out = []
    cursor = datetime.combine(day, window[0])
    limit = datetime.combine(day, window[1])
    while cursor + timedelta(minutes=minutes) <= limit and len(out) < DAY_SLOTS:
        start_utc = local_to_utc_naive(day, cursor.time())
        if hours:
            free = availability.check(assignee, start_utc, minutes, s, now=now) is None
        else:
            free = start_utc >= now and not cal.conflicts_for(assignee.id, start_utc, start_utc + timedelta(minutes=minutes))
        if free:
            out.append({"start": f"{day.isoformat()}T{cursor.strftime('%H:%M')}", "label": cursor.strftime("%H:%M")})
        cursor += timedelta(minutes=step)
    return out


def _appointment_preview(user, text: str, overrides: dict, want_slots: bool) -> dict:
    today = local_today()
    p = ci.parse(text, today)
    members = cal.office_members()

    # Kundenname: "mit X", "fuer X" (wenn kein Mitarbeiter) oder die uebrigen Woerter.
    name = p.customer_name
    if not name and p.for_name and not ci._members_matching(p.for_name, members):
        name = p.for_name
    if not name:
        name = ci.bare_name(text)
    if name and p.assignee_name is None and len(ci._members_matching(name, members)) == 1 and not ci._customers_matching(name):
        p.assignee_name, name = name, None  # "Termin Laura Donnerstag 10 Uhr" -> Mitarbeiterin

    # Mitarbeiter
    assignee, assignee_status, assignee_options = None, "ok", []
    if str(overrides.get("assignee_id") or "").isdigit():
        assignee = next((m for m in members if m.id == int(overrides["assignee_id"])), None)
    elif p.assignee_name:
        found = ci._members_matching(p.assignee_name, members)
        if len(found) == 1:
            assignee = found[0]
        else:
            assignee_status = "ambiguous" if found else "unknown"
            assignee_options = [_member_payload(m) for m in (found or members)]

    # Kunde
    customer, customer_status, customer_options = None, "none", []
    if str(overrides.get("customer_id") or "").isdigit():
        customer = Customer.query.filter(Customer.id == int(overrides["customer_id"])).first()
        customer_status = "ok" if customer else "missing"
    elif overrides.get("no_customer"):
        customer_status = "skipped"
    elif name:
        found = _find_customers(name)
        if len(found) == 1:
            customer, customer_status = found[0], "ok"
        else:
            customer_status = "ambiguous" if found else "missing"
            customer_options = [_customer_payload(c) for c in found[:MAX_CUSTOMERS]]
            if not found:
                customer_options = [_customer_payload(c) for c in ci._similar_customers(name)[:MAX_CUSTOMERS]]
    else:
        customer_status = "missing"

    if assignee is None and assignee_status == "ok":
        responsible = ci._responsible_for(customer, members)
        assignee = responsible or next((m for m in members if m.id == user.id), None)
        assignee_source = "Kundendaten" if responsible else "Sie selbst"
    else:
        assignee_source = None
    if assignee is None and assignee_status == "ok":
        assignee_status, assignee_options = "missing", [_member_payload(m) for m in members]

    # Datum / Uhrzeit (Auswahl ueberschreibt den Text)
    start = overrides.get("start")
    if isinstance(start, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}", start):
        p.day, p.start = date.fromisoformat(start[:10]), time(int(start[11:13]), int(start[14:16]))
        p.end = None
    # Dauer: genannt, Ende-Uhrzeit, Buero-Standarddauer, sonst 30 Minuten
    minutes, minutes_source = p.minutes, "genannt" if p.minutes else None
    if str(overrides.get("minutes") or "").isdigit():
        minutes, minutes_source = int(overrides["minutes"]), "gewählt"
    elif p.end is not None and p.start is not None:
        minutes, minutes_source = (p.end.hour * 60 + p.end.minute) - (p.start.hour * 60 + p.start.minute), "genannt"
    if not minutes:
        office = ci._office_default_minutes()
        minutes, minutes_source = (office, "Büro-Standard") if office else (ci.DEFAULT_MINUTES, "Standard")
    minutes_ok = 5 <= minutes <= 12 * 60

    fields = {
        "customer": {"status": customer_status, "value": _customer_payload(customer) if customer else None, "options": customer_options, "query": name},
        "day": {"status": "ok" if p.day else "missing", "value": p.day.isoformat() if p.day else None, "label": _day_label(p.day, today) if p.day else None},
        "time": {"status": "ok" if p.start else "missing", "value": p.start.strftime("%H:%M") if p.start else None},
        "assignee": {"status": assignee_status, "value": _member_payload(assignee) if assignee else None, "options": assignee_options or [_member_payload(m) for m in members], "source": assignee_source},
        "minutes": {"status": "ok" if minutes_ok else "invalid", "value": minutes, "source": minutes_source},
    }
    result = {"kind": "appointment", "fields": fields, "availability": None, "ready": False}
    if p.day and p.start and assignee is not None and minutes_ok:
        try:
            start_utc = cal.parse_local(f"{p.day.isoformat()}T{p.start.strftime('%H:%M')}", "den Beginn")
        except cal.CalendarError as exc:
            fields["time"].update(status="invalid", message=str(exc))
        else:
            result["availability"] = _availability(assignee, start_utc, minutes)
            result["start"] = f"{p.day.isoformat()}T{p.start.strftime('%H:%M')}"
    if want_slots and p.day and assignee is not None and minutes_ok:
        result["slots"] = day_slots(assignee, p.day, minutes)
    customer_done = customer_status in ("ok", "skipped")
    result["ready"] = bool(customer_done and result["availability"] and result["availability"]["status"] == "free"
                           and all(fields[k]["status"] == "ok" for k in ("day", "time", "assignee", "minutes")))
    title = f"Termin {customer.name}" if customer else (f"Termin mit {name}" if name else "Termin")
    if p.topic:
        title += f" – {p.topic}"
    result["title"] = title
    result["choice"] = {
        **({"customer_id": customer.id} if customer else {"no_customer": True} if customer_status == "skipped" else {}),
        **({"assignee_id": assignee.id} if assignee else {}),
        **({"start": result["start"]} if result.get("start") else {}),
        "minutes": minutes,
    }
    return result
