"""Kurze Folgeeingaben im Hauptchat - im Backend, ohne Modell.

- "Kuerzer": bezieht sich auf den zuletzt angezeigten Entwurf dieses Chats (Terminbestaetigung) ->
  Kurzvorlage; Datum, Uhrzeit, Ort kommen erneut aus dem Kalendertermin.
- "Doch um 16:30": offenes Terminformular -> nur die Uhrzeit im Formular aendern (Browser).
  Sonst der zuletzt in diesem Chat/Fenster gebuchte Termin bzw. der naechste Termin des sichtbar
  ausgewaehlten Kunden -> Aenderung zur Bestaetigung; gespeichert wird erst nach Klick, unter der
  Kalendersperre mit erneuter Verfuegbarkeitspruefung und Versionspruefung.
- "Die Strasse ist falsch": Adressfeld des sichtbar ausgewaehlten Kunden oeffnen; gespeichert wird
  erst nach Eingabe und "Speichern" (vorhandene Stammdatenbearbeitung mit ihren Rechten).

Eine alte Kundenkarte im Chatverlauf zaehlt nie als Auswahl. Fehlt etwas, wird nur danach gefragt
(offener Auftrag in app/services/chat_task.py); der Rest des Auftrags bleibt erhalten."""

from __future__ import annotations

import re
from datetime import datetime, time, timedelta

from app.extensions import db
from app.models import CalendarEvent, Customer
from app.services import chat_task
from app.services.chat_task import Reply
from app.services.timetracking.clock import local_to_utc_naive, to_local, utcnow_naive

_SHORTER = re.compile(
    r"^\s*(?:bitte\s+)?(?:(?:noch|etwas|viel|deutlich|ein\s+bisschen)\s+)*(?:k(?:ü|ue)rzer|knapper|kurz\s*fassen|"
    r"(?:mach|schreib)\w*\s+(?:es|ihn|sie|den\s+entwurf|die\s+mail|die\s+bestätigung)?\s*(?:noch\s+)?(?:k(?:ü|ue)rzer|knapper))"
    r"(?:\s+bitte)?\s*[.!]*\s*$",
    re.I,
)
_TIME_MARK = re.compile(r"\b(?:doch|lieber|besser|stattdessen|statt\s+dessen|verschieb\w*|(?:ä|ae)nder\w*|umlegen|sp(?:ä|ae)ter|fr(?:ü|ue)her)\b", re.I)
_BOOKING_VERB = re.compile(r"\b(?:buche\w*|neue[nr]?\s+termin|eintragen|anlegen|vereinbar\w*)\b", re.I)
FIELDS = (
    (re.compile(r"\bstra(?:ß|ss)e\b", re.I), "street", "Straße"),
    (re.compile(r"\bhaus\s*nummer\b|\bhausnr\b", re.I), "house_number", "Hausnummer"),
    (re.compile(r"\b(?:adresse|anschrift)\b", re.I), "street", "Adresse"),
    (re.compile(r"\b(?:plz|postleitzahl)\b", re.I), "postal_code", "PLZ"),
    (re.compile(r"\b(?:wohnort|ort|stadt)\b", re.I), "city", "Ort"),
    (re.compile(r"\b(?:telefon\w*|handy\w*|mobil\w*|rufnummer)\b", re.I), "phone", "Telefonnummer"),
    (re.compile(r"\b(?:e-?mail\w*|mail-?adresse)\b", re.I), "email", "E-Mail-Adresse"),
)
_WRONG = re.compile(r"\b(?:falsch|stimmt\s+nicht|nicht\s+(?:richtig|korrekt|aktuell)|veraltet|korrigier\w*|(?:ä|ae)ndern|neu\s+eintragen|hat\s+sich\s+ge(?:ä|ae)ndert)\b", re.I)


def detect(text: str) -> str | None:
    """"shorter" | "time" | "field" | None - nur kurze, eindeutige Folgeeingaben."""
    t = " ".join((text or "").split())
    if not t or len(t) > 120:
        return None
    if _SHORTER.match(t):
        return "shorter"
    if field_of(t) and _WRONG.search(t) and len(t.split()) <= 8:
        return "field"
    if _TIME_MARK.search(t) and not _BOOKING_VERB.search(t) and len(t.split()) <= 8 and _new_time(t)[1] is not None:
        return "time"
    return None


def field_of(text: str):
    for pattern, key, label in FIELDS:
        if pattern.search(text or ""):
            return key, label
    return None


def _new_time(text: str):
    from app.services import calendar_intent
    from app.services.timetracking.clock import local_today

    p = calendar_intent.parse(text, local_today())
    return p.day, p.start


def _selected(selected_id) -> Customer | None:
    return Customer.query.filter(Customer.id == int(selected_id)).first() if str(selected_id or "").isdigit() else None


def _event(event_id) -> CalendarEvent | None:
    if not str(event_id or "").isdigit():
        return None
    e = CalendarEvent.query.filter(CalendarEvent.id == int(event_id)).first()  # Mandantenfilter greift
    return e if e is not None and e.status == "scheduled" and e.ends_at >= utcnow_naive() else None


# --- "Kuerzer" -----------------------------------------------------------------------------------


def shorter(user, conversation) -> Reply:
    draft = chat_task.recall(conversation, "draft")
    if not draft or draft.get("kind") != "confirmation":
        return Reply(text="Welcher Entwurf soll kürzer werden? In diesem Chat wurde noch keine Terminbestätigung erstellt – "
                          "z. B. auf der Kundenkarte „Bestätigung kopieren“ wählen.")
    customer = Customer.query.filter(Customer.id == draft.get("customer_id")).first()
    if customer is None:
        return Reply(text="Der Kunde des letzten Entwurfs ist nicht mehr verfügbar.")
    if draft.get("event_id"):
        event = CalendarEvent.query.filter(CalendarEvent.id == draft["event_id"]).first()
        if event is None or event.status != "scheduled":
            return Reply(text="Der Termin des letzten Entwurfs wurde abgesagt oder gelöscht – es wurde kein neuer Entwurf erstellt.")
        return chat_task.compose(user, conversation, customer, "", event=event, short=True)
    start = datetime.fromisoformat(draft["start"])
    end = datetime.fromisoformat(draft["end"]) if draft.get("end") else None
    return chat_task.compose(user, conversation, customer, "", start_local=start, end_local=end, short=True)


# --- "Doch um 16:30" -----------------------------------------------------------------------------


def _proposal(user, conversation, event: CalendarEvent, day, new_time: time) -> Reply:
    """Aenderung zur Bestaetigung (noch nicht gespeichert); belegt -> Grund + freie Alternativen."""
    from app.services import availability
    from app.services import calendar as cal

    minutes = int((event.ends_at - event.starts_at).total_seconds() // 60)
    target_day = day or to_local(event.starts_at).date()
    start_utc = local_to_utc_naive(target_day, new_time)
    who = event.assignee
    reason = _busy_reason(event, start_utc, minutes)
    old = chat_task._when_label(to_local(event.starts_at), to_local(event.ends_at))
    new_label = chat_task._when_label(to_local(start_utc), to_local(start_utc + timedelta(minutes=minutes)))
    base = {"type": "change_time", "event_id": event.id, "customer_id": event.customer_id,
            "customer_name": event.customer.name if event.customer else None}
    if reason:
        from app.services.booking import _calendar_gaps, alternatives_near

        s = availability.settings()
        alts = alternatives_near(who, start_utc, minutes, s) if availability.planning_ready(s) else _calendar_gaps(who, start_utc, minutes)
        alts = [a for a in alts if not _busy_reason(event, a.start_utc, minutes)][:3]
        chat_task._save(conversation, {**base, "step": "confirm", "proposals": [a.start_utc.isoformat() for a in alts]})
        options = [{"label": f"Auf {a.label()} ändern", "sub": f"bei {cal.member_name(who)}",
                    "choice": {"reschedule": event.id, "start": a.start_utc.isoformat(), "version": event.version}} for a in alts]
        return Reply(text=f"{new_label} ist nicht möglich: {reason} Freie Alternativen (oder eine andere Uhrzeit eingeben):"
                     if options else f"{new_label} ist nicht möglich: {reason} Bitte eine andere Uhrzeit eingeben.", options=options)
    chat_task._save(conversation, {**base, "step": "confirm", "proposals": [start_utc.isoformat()]})
    title = event.title + (f" ({event.customer.name})" if event.customer and event.customer.name not in event.title else "")
    return Reply(text=f"Termin „{title}“ von {old} auf {new_label} ändern? Gespeichert wird erst nach Bestätigung.",
                 options=[{"label": f"Ja, auf {to_local(start_utc):%H:%M} Uhr ändern", "sub": f"bei {cal.member_name(who)} – Verfügbarkeit wird beim Speichern erneut geprüft",
                           "choice": {"reschedule": event.id, "start": start_utc.isoformat(), "version": event.version}}])


def _busy_reason(event: CalendarEvent, start_utc: datetime, minutes: int) -> str | None:
    from app.services import availability
    from app.services import calendar as cal

    s = availability.settings()
    if availability.planning_ready(s):
        return availability.check(event.assignee, start_utc, minutes, s, exclude_id=event.id)
    busy = cal.conflicts_for(event.assignee_user_id, start_utc, start_utc + timedelta(minutes=minutes), exclude_id=event.id)
    return f"{cal.member_name(event.assignee)} hat dann bereits „{busy[0].title}“." if busy else None


def change_time(user, conversation, text: str, *, selected_id=None, last_event_id=None) -> Reply:
    day, new_time = _new_time(text)
    if not chat_task.calendar_allowed(user):
        return Reply(text="Kein Zugriff auf den Bürokalender – der Termin kann hier nicht geändert werden.")
    remembered = chat_task.recall(conversation, "event")
    event = _event(last_event_id) or _event((remembered or {}).get("event_id"))
    if event is None:
        customer = _selected(selected_id)
        if customer is None:
            return Reply(text=f"Welcher Termin soll auf {new_time:%H:%M} Uhr geändert werden? Bitte den Kunden auswählen "
                              "(Name eingeben) oder den Termin im Kalender öffnen.")
        events = chat_task.upcoming_events(customer)
        if not events:
            return Reply(text=f"Für {customer.name} ist kein bevorstehender Termin eingetragen. Neuer Termin? Auf der Kundenkarte „Termin buchen“ wählen.")
        if len(events) > 1:
            chat_task._save(conversation, {"type": "change_time", "step": "choose_event", "customer_id": customer.id,
                                           "customer_name": customer.name, "event_ids": [e.id for e in events],
                                           "day": day.isoformat() if day else None, "time": new_time.strftime("%H:%M")})
            return Reply(text=f"Welcher Termin von {customer.name} soll auf {new_time:%H:%M} Uhr geändert werden?",
                         options=[chat_task._event_option(e) for e in events])
        event = events[0]
    return _proposal(user, conversation, event, day, new_time)


def answer_task(user, conversation, task: dict, text: str) -> Reply | None:
    """Antwort auf eine offene Rueckfrage der Typen change_time / edit_field."""
    if task.get("type") == "change_time":
        day, new_time = _new_time(text)
        if task.get("step") == "choose_event":
            event = chat_task._pick_event(task, text)
            if event is None:
                return None
            hh, mm = map(int, task["time"].split(":"))
            return _proposal(user, conversation, event, datetime.fromisoformat(task["day"]).date() if task.get("day") else None, time(hh, mm))
        if task.get("step") == "confirm" and new_time is not None and len(text.split()) <= 8:
            event = _event(task.get("event_id"))
            return _proposal(user, conversation, event, day, new_time) if event is not None else None
        return None
    if task.get("type") == "edit_field" and task.get("step") == "customer":
        found = chat_task._find_customers(text, answer=True) if chat_task._looks_like_name_answer(text) else []
        if not chat_task._looks_like_name_answer(text):
            return None
        if len(found) == 1:
            chat_task._save(conversation, None)
            return edit_reply(user, found[0], task.get("field"), task.get("label"))
        if found:
            chat_task._save(conversation, {**task, "step": "choose_customer", "candidate_ids": [c.id for c in found[:5]]})
            return Reply(text="Mehrere Kunden passen – bitte auswählen:", options=chat_task.describe(found[:5]))
        chat_task._save(conversation, task)
        return Reply(text=f"Kein Kunde „{chat_task._clean_answer(text)}“ gefunden. Bitte Namen oder VN-Nummer prüfen.")
    return None


def choose_task(user, conversation, task: dict, choice: dict) -> Reply:
    if task.get("type") == "change_time" and str(choice.get("event_id") or "").isdigit() and task.get("step") == "choose_event":
        if int(choice["event_id"]) not in (task.get("event_ids") or []):
            raise chat_task.TaskError("Diese Auswahl ist nicht mehr aktuell.")
        event = _event(choice["event_id"])
        if event is None:
            raise chat_task.TaskError("Der Termin wurde nicht gefunden.")
        hh, mm = map(int, task["time"].split(":"))
        return _proposal(user, conversation, event, datetime.fromisoformat(task["day"]).date() if task.get("day") else None, time(hh, mm))
    if task.get("type") == "change_time" and str(choice.get("reschedule") or "").isdigit() and task.get("step") == "confirm":
        if int(choice["reschedule"]) != task.get("event_id") or choice.get("start") not in (task.get("proposals") or []):
            raise chat_task.TaskError("Diese Auswahl ist nicht mehr aktuell.")
        event = _event(choice["reschedule"])
        if event is None:
            raise chat_task.TaskError("Der Termin wurde nicht gefunden oder ist bereits vorbei.")
        start_utc = datetime.fromisoformat(choice["start"])
        chat_task._save(conversation, None)
        return reschedule(user, conversation, event, start_utc, choice.get("version"))
    if task.get("type") == "edit_field" and task.get("step") == "choose_customer" and str(choice.get("customer_id") or "").isdigit():
        if int(choice["customer_id"]) not in (task.get("candidate_ids") or []):
            raise chat_task.TaskError("Diese Auswahl ist nicht mehr aktuell.")
        customer = Customer.query.filter(Customer.id == int(choice["customer_id"])).first()
        if customer is None:
            raise chat_task.TaskError("Der Kunde wurde nicht gefunden.")
        chat_task._save(conversation, None)
        return edit_reply(user, customer, task.get("field"), task.get("label"))
    raise chat_task.TaskError("Diese Auswahl ist nicht mehr aktuell.")


def reschedule(user, conversation, event: CalendarEvent, start_utc: datetime, version) -> Reply:
    """Speichern unter derselben Sperre wie jede Buchung; Verfuegbarkeit und Version erneut pruefen."""
    from app.services import calendar as cal
    from app.services.customer_lock import CustomerLockTimeout, NamedLock, fresh_transaction

    minutes = int((event.ends_at - event.starts_at).total_seconds() // 60)
    lock = NamedLock(f"zentriq-cal-{user.tenant_id}-{event.assignee_user_id}", 15)
    try:
        lock.acquire()
    except CustomerLockTimeout:
        return Reply(text="Der Kalender wird gerade geändert. Bitte gleich erneut bestätigen.")
    try:
        fresh_transaction()
        event = db.session.get(CalendarEvent, event.id)
        if str(event.version) != str(version):
            if event.starts_at == start_utc:  # Doppeltipp: bereits geaendert
                return Reply(text=f"Termin ist bereits auf {chat_task._when_label(to_local(event.starts_at), to_local(event.ends_at))} geändert.")
            return Reply(text="Der Termin wurde inzwischen geändert. Es wurde nichts gespeichert – bitte erneut „Doch um …“ eingeben.")
        reason = _busy_reason(event, start_utc, minutes)
        if reason:
            return Reply(text=f"Inzwischen nicht mehr möglich: {reason} Es wurde nichts gespeichert – bitte eine andere Uhrzeit eingeben.")
        data = {"title": event.title, "start": cal.local_iso(start_utc), "end": cal.local_iso(start_utc + timedelta(minutes=minutes)),
                "assignee_id": event.assignee_user_id, "customer_id": event.customer_id, "location_kind": event.location_kind or "",
                "location": event.location, "note": event.note, "version": event.version}
        try:
            event, _changed, _conflicts = cal.update_event(user, event, data)
        except cal.CalendarError as exc:
            return Reply(text=f"{exc} Es wurde nichts gespeichert.")
    finally:
        lock.release()
    chat_task.remember(conversation, "event", {"event_id": event.id})
    return Reply(text=f"✓ Termin geändert: {event.title}, jetzt {chat_task._when_label(to_local(event.starts_at), to_local(event.ends_at))} "
                      f"bei {cal.member_name(event.assignee)}. Es wurde nichts versendet.")


# --- "Die Strasse ist falsch" --------------------------------------------------------------------


def edit_reply(user, customer: Customer, field: str, label: str) -> Reply:
    from app.services import customer_create

    if not customer_create.can_edit(user, customer):
        return Reply(text=f"Die Kundendaten von {customer.name} darfst du nicht bearbeiten.")
    reply = Reply(text=f"{label} von {customer.name} korrigieren: neuen Wert eingeben und „Speichern“ tippen. Vorher wird nichts geändert.")
    reply.edit = {"customer_id": customer.id, "field": field}
    return reply


def edit_field(user, conversation, text: str, *, selected_id=None) -> Reply:
    field, label = field_of(text)
    customer = _selected(selected_id)
    if customer is not None:
        return edit_reply(user, customer, field, label)
    chat_task._save(conversation, {"type": "edit_field", "step": "customer", "field": field, "label": label})
    return Reply(text=f"Bei welchem Kunden ist die {label} falsch?")


def handle(user, conversation, text: str, kind: str, *, selected_id=None, last_event_id=None, form_open=False):
    """Reply oder {"kind": "form_time", ...} (Uhrzeit im offenen Terminformular)."""
    if kind == "shorter":
        return shorter(user, conversation)
    if kind == "field":
        return edit_field(user, conversation, text, selected_id=selected_id)
    if kind == "time":
        day, new_time = _new_time(text)
        if form_open:
            return {"kind": "form_time", "time": new_time.strftime("%H:%M"), "day": day.isoformat() if day else None,
                    "message": f"Uhrzeit im Terminformular auf {new_time:%H:%M} Uhr gesetzt – noch nicht gespeichert."}
        return change_time(user, conversation, text, selected_id=selected_id, last_event_id=last_event_id)
    return None
