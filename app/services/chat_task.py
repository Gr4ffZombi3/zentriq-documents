"""Offener Auftrag im ZentriqAI-Chat: Rueckfragen mit Gedaechtnis, ohne KI-Modell.

Beispiel "Schreib eine E-Mail, um den Termin zu bestaetigen":
1. Kunde: ausdruecklich genannt (voller Name, VN-/Telefon-/Kundennummer, "fuer Mueller") >
   sichtbar ausgewaehlter Kunde im Eingabefeld > Rueckfrage "Fuer welchen Kunden?".
   Eine alte Karte im Chatverlauf zaehlt nicht als Auswahl.
2. Die naechste Eingabe wird der Rueckfrage zugeordnet (nur Name/VN-Nummer bzw. Termin), mehrere
   Treffer -> Auswahl. Gleichnamige Akten werden unterscheidbar gezeigt, nie zusammengefuehrt.
3. Termin: bevorstehende Termine des Kunden im Buerokalender (verknuepft ueber die Kunden-ID;
   unverknuepfte nur bei vollem, eindeutigem Namen im Titel). Einer -> Entwurf, mehrere ->
   "Welchen Termin moechtest du bestaetigen?", keiner -> Rueckfrage nach Datum/Uhrzeit.
4. Entwurf aus einer Textvorlage (0 Modellaufrufe). Ein Modell nur bei ausdruecklichem Wunsch nach
   individueller Formulierung bzw. anderem Ton - es erhaelt dann weder Namen noch Kontaktdaten.
   Nichts wird gesendet, am Kalender aendert sich nichts.

Der Auftrag liegt in ai_conversations.pending_task (je Chat): geklaerte Angaben + der fehlende
Schritt. Ein ausdruecklich neuer Auftrag ersetzt ihn, eine andere Eingabe beendet ihn; ohne
offenen Auftrag bleibt ein allein eingegebener Name die direkte Kundensuche."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from app.extensions import db
from app.models import CalendarEvent, Customer, CustomerNote
from app.services.timetracking.clock import local_to_utc_naive, local_today, to_local, utcnow_naive

TASK_TYPE = "confirm_appointment"
TTL = timedelta(hours=6)
MAX_EVENTS = 6
WEEKDAY_NAMES = ("Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag")

ASK_CUSTOMER = "Für welchen Kunden?"
ASK_EVENT = "Welchen Termin möchtest du bestätigen?"
WAITING = {
    "customer": "Kunde für die Terminbestätigung",
    "choose_customer": "Auswahl des Kunden für die Terminbestätigung",
    "choose_event": "Auswahl des Termins für die Terminbestätigung",
    "event_text": "Datum und Uhrzeit des Termins für die Bestätigung",
}
WAITING_BY_TYPE = {
    "edit_field": {"customer": "Kunde, dessen Angabe korrigiert werden soll"},
    "change_time": {"choose_event": "Auswahl des Termins, der geändert werden soll",
                    "confirm": "Bestätigung der Terminänderung"},
}

_WRITING = re.compile(r"\b(?:schreib\w*|formulier\w*|entw[iu]rf\w*|verfass\w*|e-?mail\w*|mail|nachricht|brief)\b", re.I)
# "Termin bestaetigen", "Bestaetige den Termin", "Terminbestaetigung" - Befehl, keine Feststellung
# ("Termin bestaetigt" ist eine Notiz).
_CONFIRM_VERB = re.compile(r"\bbest(?:ä|ae)tig(?:en|e)\b", re.I)
_CONFIRM_ANY = re.compile(r"\bbest(?:ä|ae)tig\w*", re.I)
_CONFIRM_NOUN = re.compile(r"\bterminbest(?:ä|ae)tigung\w*", re.I)
_TERMIN = re.compile(r"\btermin\w*", re.I)
_CANCEL = re.compile(r"^\s*(?:abbrechen|abbruch|stopp?|vergiss\s+es|egal|doch\s+nicht|cancel)\s*[.!]*\s*$", re.I)
# Ausdruecklicher Wunsch nach eigener Formulierung / anderem Ton -> Modell (sonst Vorlage).
_TONE = re.compile(
    r"\b(?:individuell\w*|pers(?:ö|oe)nlich(?:er|en|e)?\s+formulier\w*|locker\w*|f(?:ö|oe)rmlich\w*|herzlich\w*|"
    r"freundlicher|ausf(?:ü|ue)hrlicher|tonfall|im\s+ton|anderen?\s+ton|du-form|duzen|im\s+du\b|"
    r"humorvoll\w*|eigene[nr]?\s+formulierung|anders\s+formulier\w*|kreativ\w*)\b",
    re.I,
)
_PREFIX = re.compile(r"^\s*(?:für|fuer|an|kunde|kundin|der|die|den|herrn?|frau|bei|von|mit|vn|vn-nummer)\b[:\s]*", re.I)
_NAMED = re.compile(r"\b(?:für|fuer|an|mit|von)\s+(?:(?:herrn?|frau|kunde|kundin)\s+)?([A-ZÄÖÜ][\wäöüß-]{2,}(?:\s+[A-ZÄÖÜ][\wäöüß-]{2,})?)")
_ORDINAL = {"erste": 1, "ersten": 1, "zweite": 2, "zweiten": 2, "dritte": 3, "dritten": 3, "vierte": 4, "vierten": 4,
            "fünfte": 5, "fuenfte": 5, "letzte": -1, "letzten": -1}


@dataclass
class Reply:
    text: str
    options: list[dict] = field(default_factory=list)  # {"label", "sub", "choice": {...}}
    draft: str | None = None
    status: list[str] = field(default_factory=list)
    used_model: bool = False
    edit: dict | None = None  # {"customer_id", "field"}: Stammdatenfeld zum Korrigieren oeffnen


class TaskError(Exception):
    pass


# --- Erkennung ------------------------------------------------------------------------------


def is_confirm_request(text: str) -> bool:
    """"Schreib eine E-Mail, um den Termin zu bestaetigen", "Termin bestaetigen", "Terminbestaetigung
    an Mueller". Fragen ("Ist der Termin bestaetigt?") und Feststellungen sind kein Auftrag."""
    from app.services import email_context

    t = (text or "").strip()
    if not t or len(t) > 400 or email_context.looks_like_email(t):
        return False
    writing = bool(_WRITING.search(t))
    if t.endswith("?") and not writing:
        return False
    if _CONFIRM_NOUN.search(t):
        return True
    if not _TERMIN.search(t):
        return False
    return bool(_CONFIRM_VERB.search(t)) or (writing and bool(_CONFIRM_ANY.search(t)))


# "Kuerzer"/"knapp": feste Kurzvorlage statt Modell (Datum, Uhrzeit, Ort bleiben aus dem Kalender).
_SHORT = re.compile(r"\b(?:k(?:ü|ue)rzer\w*|knapp\w*|kurz(?:e|en|er|fassen)?)\b", re.I)


def wants_tone(text: str) -> bool:
    return bool(_TONE.search(text or ""))


def wants_short(text: str) -> bool:
    return bool(_SHORT.search(text or ""))


# --- Zustand je Chat ------------------------------------------------------------------------


# Zustand je Chat in ai_conversations.pending_task (JSON):
#   {"task": offene Rueckfrage | None, "draft": zuletzt angezeigter Entwurf, "event": zuletzt gebuchter/geaenderter Termin}
# Aeltere Eintraege (nur der Auftrag auf oberster Ebene, Stand 2120f1c) werden als "task" gelesen.
TASK_TYPES = (TASK_TYPE, "edit_field", "change_time")


def _state(conversation) -> dict:
    raw = conversation.pending_task if conversation is not None else None
    if not isinstance(raw, dict):
        return {}
    if "type" in raw:  # altes Format
        return {"task": raw}
    return dict(raw)


def _write(conversation, state: dict) -> None:
    state = {k: v for k, v in state.items() if v}
    conversation.pending_task = state or None
    db.session.commit()


def _fresh(item, ttl: timedelta = TTL) -> bool:
    try:
        since = datetime.fromisoformat((item or {}).get("since") or "")
    except ValueError:
        return False
    return utcnow_naive() - since <= ttl


def get(conversation) -> dict | None:
    task = _state(conversation).get("task")
    if not isinstance(task, dict) or task.get("type") not in TASK_TYPES:
        return None
    if not _fresh(task):
        _write(conversation, {**_state(conversation), "task": None})
        return None
    return task


def _save(conversation, task: dict | None) -> None:
    if task is not None:
        task = {**task, "type": task.get("type") or TASK_TYPE, "since": utcnow_naive().isoformat()}
    _write(conversation, {**_state(conversation), "task": task})


def clear(conversation) -> bool:
    had = get(conversation) is not None
    if had:
        _write(conversation, {**_state(conversation), "task": None})
    return had


def remember(conversation, key: str, value: dict | None) -> None:
    """Kontext fuer kurze Folgeeingaben merken ("draft": Entwurf fuer "Kuerzer", "event": Termin
    fuer "Doch um 16:30")."""
    if conversation is None:
        return
    _write(conversation, {**_state(conversation), key: ({**value, "since": utcnow_naive().isoformat()} if value else None)})


def recall(conversation, key: str) -> dict | None:
    item = _state(conversation).get(key)
    return item if isinstance(item, dict) and _fresh(item, timedelta(hours=12)) else None


def public(conversation) -> dict | None:
    """Was der Chat anzeigt: worauf Zentriq wartet (mit "Abbrechen")."""
    task = get(conversation)
    if task is None:
        return None
    label = (WAITING_BY_TYPE.get(task.get("type"), {}) or WAITING).get(task.get("step")) or "Angabe zum offenen Auftrag"
    if task.get("customer_name") and task.get("step") in ("choose_event", "event_text", "confirm"):
        label += f" ({task['customer_name']})"
    return {"label": label, "step": task.get("step")}


# --- Kunden ---------------------------------------------------------------------------------


def _clean_answer(text: str) -> str:
    t = " ".join((text or "").split()).strip(" .,!")
    for _ in range(3):
        t2 = _PREFIX.sub("", t)
        if t2 == t:
            break
        t = t2
    return t.strip(" .,!")


def _find_customers(text: str, *, answer: bool) -> list[Customer]:
    """answer=True: die Eingabe ist die Antwort auf "Fuer welchen Kunden?" (Name/VN-Nummer allein);
    sonst ein im Auftrag genannter Kunde (nur eindeutig erkennbare Nennungen)."""
    from app.services import customer_notes as cn
    from app.services.memo_customer_match import _by_name

    parsed = cn.parse(text)
    basis, found = cn.find_by_identifiers(parsed)
    if found:
        return found
    if basis is not None and answer:
        return []
    by_name = _by_name(text)
    if by_name:
        return by_name
    if answer:
        cleaned = _clean_answer(text)
        found = cn.search_by_name_fragment(cleaned)
        return found
    m = _NAMED.search(text or "")
    if m:
        return cn.search_by_name_fragment(m.group(1))
    return []


def _looks_like_name_answer(text: str) -> bool:
    """Kurze Antwort, die ein Name bzw. eine Nummer sein kann (sonst: neuer Auftrag)."""
    from app.services import customer_notes as cn

    t = _clean_answer(text)
    if not t or len(t) > 80 or "?" in t:
        return False
    if cn.parse(t).has_identifier:
        return True
    words = t.split()
    return 1 <= len(words) <= 3 and all(re.fullmatch(r"[A-Za-zÄÖÜäöüßÉéÈèÁáÓóÚúÇç'’-]{2,}", w) for w in words)


def describe(customers: list[Customer]) -> list[dict]:
    """Auswahl mit unterscheidenden Angaben; gleichnamige Akten zusaetzlich mit Aktennummer,
    Notizen und Terminen (nie automatisch zusammengefuehrt)."""
    from app.services.customer_notes import serialize_candidate

    names = [c.name.strip().lower() for c in customers]
    out = []
    for c in customers:
        data = serialize_candidate(c)
        parts = [data.get("vn"), data.get("place"), f"geb. {data['birth']}" if data.get("birth") else None, data.get("phone")]
        if names.count(c.name.strip().lower()) > 1:
            notes = CustomerNote.query.filter(CustomerNote.customer_id == c.id).count()
            events = len(upcoming_events(c, limit=MAX_EVENTS))
            parts += [f"Akte {c.id}", f"{notes} Notiz" if notes == 1 else f"{notes} Notizen",
                      "1 bevorstehender Termin" if events == 1 else f"{events} bevorstehende Termine"]
        out.append({"label": c.name, "sub": " · ".join(p for p in parts if p) or "keine weiteren Angaben",
                    "choice": {"customer_id": c.id}})
    return out


# --- Termine --------------------------------------------------------------------------------


def calendar_allowed(user) -> bool:
    from app.services.email_reply import _calendar_allowed

    return _calendar_allowed(user)


def upcoming_events(customer: Customer, limit: int = MAX_EVENTS) -> list[CalendarEvent]:
    """Bevorstehende Termine dieses Kunden (Kunden-ID). Unverknuepfte Termine zaehlen nur, wenn ihr
    Titel den vollen Namen enthaelt und keine andere Akte so heisst."""
    from app.utils.customer_keys import name_parts

    now = utcnow_naive()
    base = CalendarEvent.query.filter(CalendarEvent.status == "scheduled", CalendarEvent.ends_at >= now)
    events = base.filter(CalendarEvent.customer_id == customer.id).order_by(CalendarEvent.starts_at).limit(limit).all()
    parts = (customer.name_key or "").split()
    if len(parts) >= 2 and Customer.query.filter(Customer.name_key == customer.name_key).count() == 1:
        loose = (base.filter(CalendarEvent.customer_id.is_(None), CalendarEvent.starts_at <= now + timedelta(days=365))
                 .order_by(CalendarEvent.starts_at).limit(200).all())
        events += [e for e in loose if all(p in set(name_parts(e.title)) for p in parts)]
    events.sort(key=lambda e: e.starts_at)
    return events[:limit]


def _when_label(start_local: datetime, end_local: datetime | None = None) -> str:
    day = f"{WEEKDAY_NAMES[start_local.weekday()]}, {start_local:%d.%m.%Y}"
    clock = f"{start_local:%H:%M}" + (f"–{end_local:%H:%M}" if end_local and end_local > start_local else "") + " Uhr"
    return f"{day}, {clock}"


def _event_option(event: CalendarEvent) -> dict:
    from app.services.calendar import LOCATION_KINDS, member_name

    start, end = to_local(event.starts_at), to_local(event.ends_at)
    sub = " · ".join(p for p in (LOCATION_KINDS.get(event.location_kind or ""), event.location, event.title,
                                 f"bei {member_name(event.assignee)}" if event.assignee else None) if p)
    return {"label": _when_label(start, end), "sub": sub, "choice": {"event_id": event.id}}


# --- Entwurf --------------------------------------------------------------------------------

DETAILS_TOKEN = "[[TERMINDATEN]]"
TONE_SYSTEM = (
    "Du formulierst für ein deutsches Versicherungsbüro den Mitteltext einer kurzen E-Mail, die einem Kunden "
    "einen bereits vereinbarten Termin bestätigt. Schreibe NUR die Absätze zwischen Anrede und Grußformel: keine "
    "Anrede, keine Grußformel, keine Signatur, keinen Betreff, keine Namen, keine Kontaktdaten. Setze genau einmal "
    f"den Platzhalter {DETAILS_TOKEN} als eigene Zeile dorthin, wo Datum, Uhrzeit und Ort stehen sollen. Erfinde "
    "keine Fakten, Zusagen oder Unterlagen. Halte dich an den gewünschten Ton."
)


def _details(start_local: datetime, end_local: datetime | None, kind: str | None, location: str | None) -> str:
    from app.services.calendar import LOCATION_KINDS

    lines = [f"Datum: {WEEKDAY_NAMES[start_local.weekday()]}, {start_local:%d.%m.%Y}",
             f"Uhrzeit: {start_local:%H:%M}" + (f" – {end_local:%H:%M}" if end_local and end_local > start_local else "") + " Uhr"]
    if kind in LOCATION_KINDS:
        lines.append(f"Terminart: {LOCATION_KINDS[kind]}")
    if location:
        lines.append(f"Ort: {location}")
    return "\n".join(lines)


def _short_body(start_local: datetime, end_local: datetime | None, kind: str | None, location: str | None) -> str:
    from app.services.calendar import LOCATION_KINDS

    where = ", ".join(p for p in (LOCATION_KINDS.get(kind or ""), location) if p)
    return f"ich bestätige unseren Termin am {_when_label(start_local, end_local)}" + (f" ({where})" if where else "") + "."


def _template_body() -> str:
    return ("hiermit bestätige ich Ihnen gern unseren Termin:\n\n" + DETAILS_TOKEN +
            "\n\nSollte Ihnen der Termin doch nicht passen, geben Sie mir bitte kurz Bescheid.")


def _model_body(user, conversation, instruction: str, customer: Customer, facts: str) -> str | None:
    """Nur auf ausdruecklichen Wunsch. An das Modell gehen Ton/Wunsch (ohne Kundennamen) und die
    Termin-Eckdaten ohne Ort - keine Namen, keine Kontaktdaten."""
    from app.services.ai import router
    from app.utils.customer_keys import name_parts

    if not router.can_answer():
        return None
    wish = instruction
    for word in {*(customer.name or "").split(), *(w.capitalize() for w in name_parts(customer.name))}:
        if len(word) >= 2:
            wish = re.sub(rf"\b{re.escape(word)}\b", "[Kunde]", wish, flags=re.I)
    try:
        completion = router.complete(
            system=TONE_SYSTEM,
            messages=[{"role": "user", "content": f"Wunsch des Mitarbeiters: {wish[:300]}\n\nTermin (nur zur Orientierung): {facts}"}],
            user_id=user.id, tenant_id=conversation.tenant_id if conversation is not None else user.tenant_id,
            choice=conversation.model_choice if conversation is not None else None, customer_data=False,
            conversation_id=conversation.id if conversation is not None else None, purpose="appointment_confirmation",
            max_tokens=600, complex_task=False,
        )
    except router.AiError:
        return None
    return completion.text or None


def compose(user, conversation, customer: Customer, instruction: str, *, event: CalendarEvent | None = None,
            start_local: datetime | None = None, end_local: datetime | None = None, short: bool | None = None) -> Reply:
    from app.models import OfficeSettings
    from app.services import email_reply
    from app.services.calendar import member_name

    s = OfficeSettings.query.first()
    kind = location = None
    if event is not None:
        start_local, end_local = to_local(event.starts_at), to_local(event.ends_at)
        kind, location = event.location_kind, event.location
    details = _details(start_local, end_local, kind, location)
    reply = Reply(text="", status=[f"Kunde: {customer.name}"])
    if event is not None:
        reply.status.append(f"Termin aus dem Bürokalender: {_when_label(start_local, end_local)}"
                            + (f" bei {member_name(event.assignee)}" if event.assignee else ""))
    else:
        reply.status.append(f"Termin laut deiner Angabe: {_when_label(start_local)} (nicht im Kalender gefunden, dort nichts geändert)")
    body = None
    warning = None
    short = wants_short(instruction) if short is None else short
    if short:
        body = _short_body(start_local, end_local, kind, location)
        details = None
    elif wants_tone(instruction):
        body = _model_body(user, conversation, instruction, customer, _when_label(start_local, end_local))
        if body:
            body = email_reply._clean_model_body(body, email_reply._info_from_customer(customer), s.email if s else None)
            reply.used_model = bool(body)
        if not body:
            warning = "Individuelle Formulierung ist gerade nicht möglich – Standardtext verwendet."
    body = body or _template_body()
    if details is not None:
        body = body.replace(DETAILS_TOKEN, details, 1).replace(DETAILS_TOKEN, "") if DETAILS_TOKEN in body else body.rstrip() + "\n\n" + details
    subject = f"Terminbestätigung: {WEEKDAY_NAMES[start_local.weekday()]}, {start_local:%d.%m.%Y}, {start_local:%H:%M} Uhr"
    info = email_reply._info_from_customer(customer)
    recipient = f"An: {customer.email}\n" if customer.email else ""
    signature = f"Viele Grüße\n{member_name(user)}" if short else email_reply._signature(user, s)
    reply.draft = f"{recipient}Betreff: {subject}\n\n{email_reply._greeting(info)}\n\n{body.strip()}\n\n{signature}"
    # Fuer Folgeeingaben ("Kuerzer"): worauf sich der zuletzt angezeigte Entwurf bezieht.
    remember(conversation, "draft", {"kind": "confirmation", "customer_id": customer.id, "event_id": event.id if event else None,
                                     "start": start_local.isoformat() if event is None else None,
                                     "end": end_local.isoformat() if event is None and end_local else None, "short": bool(short)})
    if warning:
        reply.status.append("⚠ " + warning)
    reply.text = "Entwurf – nicht gesendet, keine Änderung am Kalender."
    return reply


# --- Ablauf ---------------------------------------------------------------------------------


def _for_customer(user, conversation, task: dict, customer: Customer) -> Reply:
    task = {**task, "customer_id": customer.id, "customer_name": customer.name, "candidate_ids": None}
    if not calendar_allowed(user):
        _save(conversation, {**task, "step": "event_text", "event_ids": None})
        return Reply(text=f"Kein Zugriff auf den Bürokalender. Für welchen Termin mit {customer.name}? "
                          "Bitte Datum und Uhrzeit angeben, z. B. „12.11. um 14 Uhr“.")
    events = upcoming_events(customer)
    if len(events) == 1:
        _save(conversation, None)
        return compose(user, conversation, customer, task.get("instruction") or "", event=events[0])
    if events:
        _save(conversation, {**task, "step": "choose_event", "event_ids": [e.id for e in events]})
        return Reply(text=f"{ASK_EVENT} ({customer.name})", options=[_event_option(e) for e in events])
    _save(conversation, {**task, "step": "event_text", "event_ids": None})
    return Reply(text=f"Im Bürokalender ist kein bevorstehender Termin mit {customer.name} eingetragen. Welchen Termin "
                      "möchtest du bestätigen? Bitte Datum und Uhrzeit angeben, z. B. „12.11. um 14 Uhr“.")


def _choose_customers(conversation, task: dict, customers: list[Customer], message: str) -> Reply:
    from app.services.customer_notes import MAX_CANDIDATES

    shown = customers[:MAX_CANDIDATES]
    _save(conversation, {**task, "step": "choose_customer", "candidate_ids": [c.id for c in shown]})
    more = " Weitere Treffer – bitte genauer eingeben (z. B. VN-Nummer)." if len(customers) > MAX_CANDIDATES else ""
    return Reply(text=message + more, options=describe(shown))


def start(user, conversation, text: str, selected_id=None) -> Reply:
    task = {"step": "customer", "instruction": text[:400]}
    named = _find_customers(text, answer=False)
    if len(named) == 1:
        return _for_customer(user, conversation, task, named[0])
    if len(named) > 1:
        return _choose_customers(conversation, task, named, "Mehrere Kunden passen – für welchen Kunden?")
    if str(selected_id or "").isdigit():
        selected = Customer.query.filter(Customer.id == int(selected_id)).first()
        if selected is not None:
            return _for_customer(user, conversation, task, selected)
    _save(conversation, task)
    return Reply(text=ASK_CUSTOMER)


def _pick_event(task: dict, text: str) -> CalendarEvent | None:
    """Antwort auf "Welchen Termin?": Nummer/Ordnungszahl oder Datum/Uhrzeit eines angebotenen Termins."""
    from app.services import calendar_intent

    ids = task.get("event_ids") or []
    events = [e for e in CalendarEvent.query.filter(CalendarEvent.id.in_(ids or [0])).all()]
    events.sort(key=lambda e: e.starts_at)
    t = text.strip().lower().strip(".")
    index = None
    if re.fullmatch(r"\d", t):
        index = int(t)
    else:
        m = re.search(r"\b(" + "|".join(_ORDINAL) + r")\b", t)
        if m and len(t.split()) <= 3:
            index = _ORDINAL[m.group(1)]
    if index is not None:
        if index == -1:
            return events[-1] if events else None
        return events[index - 1] if 1 <= index <= len(events) else None
    p = calendar_intent.parse(text, local_today())
    if p.day is None and p.start is None:
        return None
    hits = [e for e in events if (p.day is None or to_local(e.starts_at).date() == p.day)
            and (p.start is None or to_local(e.starts_at).time().replace(second=0) == p.start)]
    return hits[0] if len(hits) == 1 else None


def answer(user, conversation, task: dict, text: str) -> Reply | None:
    """Eingabe als Antwort auf die offene Rueckfrage. None: keine Antwort, sondern ein neuer Auftrag."""
    from app.services import chat_followup

    if task.get("type") not in (None, TASK_TYPE):
        return chat_followup.answer_task(user, conversation, task, text)
    step = task.get("step")
    if step in ("customer", "choose_customer"):
        # "Kuerzer", "Die Strasse ist falsch", "Doch um 16:30" sind keine Kundennamen.
        if not _looks_like_name_answer(text) or chat_followup.detect(text):
            return None
        found = _find_customers(text, answer=True)
        if len(found) == 1:
            return _for_customer(user, conversation, task, found[0])
        if found:
            return _choose_customers(conversation, task, found, f"Mehrere Kunden passen zu „{_clean_answer(text)}“ – bitte auswählen:")
        from app.services.customer_notes import similar_customers

        similar = similar_customers(_clean_answer(text))
        if similar:
            return _choose_customers(conversation, task, similar, f"Kein Kunde „{_clean_answer(text)}“. Meintest du:")
        _save(conversation, task)
        return Reply(text=f"Kein Kunde „{_clean_answer(text)}“ in deinem Büro gefunden. Bitte Namen oder VN-Nummer prüfen.")
    customer = Customer.query.filter(Customer.id == task.get("customer_id")).first()
    if customer is None:
        clear(conversation)
        return Reply(text="Der Kunde ist nicht mehr verfügbar. Der Auftrag wurde beendet.")
    if step == "choose_event":
        event = _pick_event(task, text)
        if event is None:
            return None
        _save(conversation, None)
        return compose(user, conversation, customer, task.get("instruction") or "", event=event)
    if step == "event_text":
        from app.services import calendar_intent

        p = calendar_intent.parse(text, local_today())
        if p.day is None or p.start is None:
            if p.day is not None or p.start is not None:
                _save(conversation, task)
                return Reply(text="Bitte Datum und Uhrzeit angeben, z. B. „12.11. um 14 Uhr“.")
            return None
        start_local = to_local(local_to_utc_naive(p.day, p.start))
        end_local = to_local(local_to_utc_naive(p.day, p.end)) if p.end else None
        _save(conversation, None)
        return compose(user, conversation, customer, task.get("instruction") or "", start_local=start_local, end_local=end_local)
    return None


def choose(user, conversation, task: dict | None, choice: dict) -> Reply:
    """Klick auf eine angebotene Auswahl - nur gueltig fuer den aktuell offenen Auftrag."""
    if task is None:
        raise TaskError("Diese Auswahl ist nicht mehr aktuell – der Auftrag ist bereits erledigt oder abgebrochen.")
    if task.get("type") not in (None, TASK_TYPE):
        from app.services import chat_followup

        return chat_followup.choose_task(user, conversation, task, choice)
    if task.get("step") == "choose_customer" and str(choice.get("customer_id") or "").isdigit():
        cid = int(choice["customer_id"])
        if cid not in (task.get("candidate_ids") or []):
            raise TaskError("Diese Auswahl ist nicht mehr aktuell.")
        customer = Customer.query.filter(Customer.id == cid).first()
        if customer is None:
            raise TaskError("Der Kunde wurde nicht gefunden.")
        return _for_customer(user, conversation, task, customer)
    if task.get("step") == "choose_event" and str(choice.get("event_id") or "").isdigit():
        eid = int(choice["event_id"])
        if eid not in (task.get("event_ids") or []):
            raise TaskError("Diese Auswahl ist nicht mehr aktuell.")
        event = CalendarEvent.query.filter(CalendarEvent.id == eid).first()
        customer = Customer.query.filter(Customer.id == task.get("customer_id")).first()
        if event is None or customer is None:
            raise TaskError("Der Termin wurde nicht gefunden.")
        _save(conversation, None)
        return compose(user, conversation, customer, task.get("instruction") or "", event=event)
    raise TaskError("Diese Auswahl ist nicht mehr aktuell.")


def dispatch(user, conversation, text: str, *, selected_id=None, choice=None) -> tuple[Reply | None, bool]:
    """(Antwort, Auftrag beendet). Antwort None: normale Verarbeitung (Suche, Notiz, Kalender, KI)."""
    task = get(conversation)
    if isinstance(choice, dict):
        return choose(user, conversation, task, choice), False
    if task is not None and _CANCEL.match(text or ""):
        clear(conversation)
        return Reply(text="Abgebrochen – es wurde nichts erstellt."), True
    if is_confirm_request(text):
        return start(user, conversation, text, selected_id=selected_id), False
    if task is None:
        return None, False
    reply = answer(user, conversation, task, text)
    if reply is None:
        clear(conversation)  # andere Eingabe = neuer Auftrag; der alte endet
        return None, True
    return reply, False


def render(reply: Reply) -> str:
    """Als Chat-Nachricht (Verlauf): Status, Rueckfrage mit Auswahl, kopierbarer Entwurf."""
    lines = [line if line.startswith("⚠") else f"✓ {line}" for line in reply.status]
    if reply.text:
        lines.append(reply.text)
    text = "\n\n".join(lines)
    if reply.options:
        text += "\n\n" + "\n".join(f"{i}. {o['label']}" + (f" – {o['sub']}" if o.get("sub") else "") for i, o in enumerate(reply.options, 1))
    if reply.draft:
        text += "\n\n```E-Mail-Entwurf\n" + reply.draft.replace("```", "'''") + "\n```"
    return text
