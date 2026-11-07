"""Termine ueber das Eingabefeld des Assistenten ("Termin mit Max Mustermann Donnerstag um 14 Uhr,
30 Minuten, bei Laura").

Deterministische Auswertung (kein KI-Modell): Datum, Uhrzeit, Dauer, zustaendiger Mitarbeiter,
Kunde, Art. Unklare Personen oder fehlende Pflichtangaben -> kurze Rueckfrage mit Auswahl; es wird
erst gespeichert, wenn alles eindeutig ist, und erst nach erfolgreicher Speicherung bestaetigt.
Es werden keine Einladungen oder Nachrichten verschickt."""

import re
from dataclasses import dataclass, field
from datetime import date, time, timedelta
from urllib.parse import urlencode

from flask import url_for

from app.models import Customer
from app.services import calendar as cal
from app.services.timetracking.clock import local_today, to_local, utcnow_naive

WEEKDAYS = {
    "montag": 0, "dienstag": 1, "mittwoch": 2, "donnerstag": 3, "freitag": 4, "samstag": 5, "sonntag": 6,
    "mo": 0, "di": 1, "mi": 2, "do": 3, "fr": 4, "sa": 5, "so": 6,
}
_CREATE = re.compile(r"^\s*(?:bitte\s+)?(?:(?:einen?|neuen?)\s+)?termin\b(?!e)", re.I)
_CREATE_VERB = re.compile(r"\btermin\b(?!e).*\b(eintragen|anlegen|vereinbaren|machen|ausmachen|planen)\b", re.I)
# Ausdrueckliche Buchung: "Buche Dienstag um 14 Uhr fuer Max Mustermann bei Laura" - Verfuegbarkeit
# wird unmittelbar vor dem Speichern geprueft (keine Doppelbuchung, Buerozeiten).
_BOOK = re.compile(r"^\s*(?:bitte\s+)?(?:buche|buchen|reserviere)\b|\btermin\b(?!e).*\bbuchen\b", re.I)
_LIST = re.compile(r"^\s*(?:welche|was\s+f[üu]r|habe?\s+ich|hat\s+\w+|gibt\s+es)\b.*\btermine?\b|^\s*(?:meine\s+)?termine\s+(?:heute|morgen|diese\s+woche)\s*\??$", re.I)

_DATE_NUM = re.compile(r"\b(?:am\s+)?(\d{1,2})\.(\d{1,2})\.(\d{2,4})?")
_REL_DAY = re.compile(r"\b(heute|morgen|übermorgen|uebermorgen)\b", re.I)
_WEEKDAY = re.compile(r"\b(?:(nächste[nmr]?|naechste[nmr]?|kommende[nmr]?)\s+(?:woche\s+)?)?(?:am\s+)?(montag|dienstag|mittwoch|donnerstag|freitag|samstag|sonntag)\b", re.I)
_NEXT_WEEK = re.compile(r"\b(?:nächste|naechste|kommende)\s+woche\b", re.I)
_TIME_RANGE = re.compile(r"\b(?:von\s+)?(\d{1,2})(?:[:.](\d{2}))?\s*(?:uhr)?\s*(?:bis|-|–)\s*(\d{1,2})(?:[:.](\d{2}))?\s*uhr\b", re.I)
_TIME = re.compile(r"\bum\s+(\d{1,2})(?:[:.](\d{2}))?\s*(?:uhr)?\b|\b(\d{1,2})[:.](\d{2})\s*(?:uhr)?\b|\b(\d{1,2})\s*uhr\b", re.I)
_DURATION = re.compile(r"\b(\d+(?:[.,]\d+)?)\s*(minuten|minute|min\.?|stunden|stunde|std\.?|h)\b", re.I)
_DURATION_WORDS = (
    (re.compile(r"\b(?:eine\s+)?halbe\s+stunde\b", re.I), 30),
    (re.compile(r"\b(?:anderthalb|eineinhalb)\s+stunden?\b", re.I), 90),
    (re.compile(r"\beine\s+stunde\b", re.I), 60),
    (re.compile(r"\bzwei\s+stunden\b", re.I), 120),
)
_ASSIGNEE = re.compile(r"\b(?:bei|für|fuer|zuständig(?:\s+ist)?|zustaendig(?:\s+ist)?)\s+([A-ZÄÖÜa-zäöüß][\wäöüß-]+(?:\s+[A-ZÄÖÜ][\wäöüß-]+)?)", re.I)
_ASSIGNEE_BEI = re.compile(r"\b(?:bei|zuständig(?:\s+ist)?|zustaendig(?:\s+ist)?)\s+([A-ZÄÖÜa-zäöüß][\wäöüß-]+(?:\s+[A-ZÄÖÜ][\wäöüß-]+)?)", re.I)
_FOR = re.compile(r"\b(?:für|fuer)\s+(?:(?:herrn?|frau|kunde|kundin)\s+)?([A-ZÄÖÜa-zäöüß][\wäöüß-]+(?:\s+[A-ZÄÖÜ][\wäöüß-]+){0,2})")
_CUSTOMER = re.compile(r"\bmit\s+(?:(?:herrn?|frau|kunde|kundin)\s+)?([A-ZÄÖÜ][\wäöüß-]+(?:\s+[A-ZÄÖÜ][\wäöüß-]+){0,2})")
_TOPIC = re.compile(r"\b(?:wegen|zum\s+thema|thema|betreff|zu[mr]?)\s*:?\s+(.+?)(?=[,.;]|$|\s+(?:am|um|bei|für|mit|heute|morgen)\b)", re.I)
_KINDS = (
    (re.compile(r"\b(?:telefonisch|telefon|anruf|rückruf|rueckruf)\b", re.I), "telefon"),
    (re.compile(r"\b(?:video|videocall|teams|zoom)\b", re.I), "video"),
    (re.compile(r"\b(?:vor\s+ort|hausbesuch|im\s+büro|im\s+buero|beim\s+kunden)\b", re.I), "vor_ort"),
)
DEFAULT_MINUTES = 30
_PRONOUNS = {"mir", "mich", "uns", "ihm", "ihr", "ihnen", "dem", "der", "den"}

# Kurzer Buchungsauftrag ("09.11.26 Schmidthofer Termin um 16.30"): Datum + Uhrzeit + "Termin",
# keine Frage, kein Schreibauftrag. Fragen ("Ist am 09.11. um 16:30 noch frei?") und Mail-
# Auftraege ("Schreib eine Mail mit Terminvorschlaegen") buchen nie.
_TERMIN_WORD = re.compile(r"\btermin\b(?!e)", re.I)
_WRITING = re.compile(r"\b(?:schreib\w*|formulier\w*|entw[uü]rf\w*|e-?mail\w*|mail|nachricht|antwort\w*|brief|vorschl[äa]g\w*|vorschlagen|anbieten)\b", re.I)
_QUESTION = re.compile(r"\?\s*$|^\s*(?:ist|sind|gibt|hat|habe|haben|hast|wann|welche[rsmn]?|wer|was|wie|kann|k[öo]nnte?n?|geht|passt|w[äa]re|bin|bist)\b", re.I)
_AVAILABILITY = re.compile(r"\b(?:frei|verf[üu]gbar|belegt|besetzt|zeit|passt|geht|m[öo]glich)\b", re.I)
_NAME_STOP = {
    "termin", "um", "uhr", "am", "bitte", "heute", "morgen", "übermorgen", "uebermorgen", "mit", "bei", "für", "fuer", "und",
    "telefonisch", "telefon", "anruf", "video", "vor", "ort", "hausbesuch", "im", "büro", "buero", "beim", "kunden", "herr",
    "herrn", "frau", "kunde", "kundin", "minuten", "minute", "min", "stunde", "stunden", "std", "h", "nächste", "naechste",
    "nächsten", "naechsten", "kommende", "kommenden", "woche", "buche", "buchen", "eintragen", "anlegen", "vereinbaren",
}


def _has_when(text: str) -> bool:
    p = parse(text, local_today())
    return p.day is not None and p.start is not None


def is_booking_command(text: str) -> bool:
    """Kurzer Buchungsauftrag mit Datum und Uhrzeit ("09.11.26 Schmidthofer Termin um 16.30") -
    lokal erkannt, ohne KI. Wird wie "Buche ..." gebucht (Verfuegbarkeit beim Speichern)."""
    if _CREATE.search(text) or _CREATE_VERB.search(text) or _BOOK.search(text):
        return False  # bisherige Formen ("Termin mit ...", "... eintragen", "Buche ...") wie gehabt
    return bool(_TERMIN_WORD.search(text) and not _QUESTION.search(text) and not _WRITING.search(text) and _has_when(text))


def is_availability_question(text: str) -> bool:
    """"Ist am 09.11. um 16:30 noch frei?" - wird beantwortet, bucht aber nie."""
    return bool(_QUESTION.search(text) and _AVAILABILITY.search(text) and not _WRITING.search(text) and _has_when(text))


def bare_name(text: str) -> str | None:
    """Name ohne "mit"/"fuer" ("09.11.26 Schmidthofer Termin um 16.30"): uebrige grossgeschriebene
    Woerter nach Entfernen von Datum, Uhrzeit, Dauer und Fuellwoertern."""
    rest = _DATE_NUM.sub(" ", text)
    rest = _TIME_RANGE.sub(" ", rest)
    rest = _TIME.sub(" ", rest)
    rest = _DURATION.sub(" ", rest)
    rest = _ASSIGNEE_BEI.sub(" ", rest)
    rest = _TOPIC.sub(" ", rest)
    words = []
    for word in re.findall(r"[A-Za-zÄÖÜäöüß][\wäöüß-]*", rest):
        low = word.lower()
        if low in _NAME_STOP or low in WEEKDAYS or not word[0].isupper():
            continue
        words.append(word)
    return " ".join(words[:3]) or None


def _office_default_minutes() -> int | None:
    """Standarddauer aus den Buero-Einstellungen (/kalender/einstellungen); None = nicht hinterlegt."""
    from app.services import availability

    s = availability.settings()
    return int(s.default_minutes) if s is not None and s.default_minutes else None


def _broker_key(value) -> str:
    return re.sub(r"[^0-9a-z]", "", (value or "").lower())


def _responsible_for(customer, members):
    """Zustaendig laut Kundendaten: Betreuer (assigned_user_id), sonst Mitarbeiter mit der
    Vermittlernummer des Kunden. None, wenn nichts (eindeutig) hinterlegt ist."""
    if customer is None:
        return None
    if customer.assigned_user_id:
        found = next((m for m in members if m.id == customer.assigned_user_id), None)
        if found is not None:
            return found
    key = _broker_key(customer.broker_number)
    if key:
        found = [m for m in members if _broker_key(getattr(m, "vermittlernummer", None)) == key]
        if len(found) == 1:
            return found[0]
    return None


def is_calendar_request(text: str, customer_selected: bool = False) -> bool:
    """Bei sichtbar ausgewaehltem Kunden bleibt eine kurze Notiz wie "Termin Donnerstag 14 Uhr"
    eine Kundennotiz (bisheriges Verhalten). Ein Termin wird es dort nur mit ausdruecklichem
    Auftrag: Verb ("eintragen", "anlegen" ...), Person ("mit ...") oder Zustaendigem ("bei ...")."""
    if _LIST.search(text) or _CREATE_VERB.search(text) or _BOOK.search(text):
        return True
    if is_availability_question(text):
        return True
    if is_booking_command(text):
        # Bei ausgewaehltem Kunden ohne Namen im Text bleibt "Termin 09.11. 14 Uhr" eine Notiz.
        return not customer_selected or bool(bare_name(text) or _CUSTOMER.search(text) or _ASSIGNEE.search(text))
    if not _CREATE.search(text):
        return False
    return not customer_selected or bool(_CUSTOMER.search(text) or _ASSIGNEE.search(text))


@dataclass
class Parsed:
    day: date | None = None
    start: time | None = None
    end: time | None = None
    minutes: int | None = None
    assignee_name: str | None = None
    customer_name: str | None = None
    kind: str | None = None
    topic: str | None = None
    notes: list[str] = field(default_factory=list)
    for_name: str | None = None  # "fuer X": Kunde (oder Mitarbeiter, falls X einer ist)
    booking: bool = False


def _next_weekday(today: date, weekday: int, next_week: bool) -> date:
    if next_week:
        monday_next = today - timedelta(days=today.weekday()) + timedelta(days=7)
        return monday_next + timedelta(days=weekday)
    delta = (weekday - today.weekday()) % 7
    return today + timedelta(days=delta or 7)


def _valid_time(h, m) -> time | None:
    h, m = int(h), int(m or 0)
    return time(h, m) if 0 <= h <= 23 and 0 <= m <= 59 else None


def parse(text: str, today: date) -> Parsed:
    p = Parsed()
    rest = text
    match = _DATE_NUM.search(rest)
    if match:
        d, mth, y = int(match.group(1)), int(match.group(2)), match.group(3)
        year = int(y) + (2000 if y and len(y) == 2 else 0) if y else today.year
        try:
            candidate = date(year, mth, d)
            if not y and candidate < today:
                candidate = date(year + 1, mth, d)
            p.day = candidate
        except ValueError:
            p.notes.append("Das Datum ist ungültig.")
        rest = rest[: match.start()] + " " + rest[match.end():]
    if p.day is None:
        rel = _REL_DAY.search(rest)
        if rel:
            word = rel.group(1).lower()
            p.day = today + timedelta(days={"heute": 0, "morgen": 1}.get(word, 2))
        else:
            wd = _WEEKDAY.search(rest)
            if wd:
                p.day = _next_weekday(today, WEEKDAYS[wd.group(2).lower()], bool(wd.group(1)) or bool(_NEXT_WEEK.search(rest)))
    rng = _TIME_RANGE.search(rest)
    if rng:
        p.start, p.end = _valid_time(rng.group(1), rng.group(2)), _valid_time(rng.group(3), rng.group(4))
        rest = rest[: rng.start()] + " " + rest[rng.end():]
    else:
        tm = _TIME.search(rest)
        if tm:
            h, m = (tm.group(1), tm.group(2)) if tm.group(1) else ((tm.group(3), tm.group(4)) if tm.group(3) else (tm.group(5), None))
            p.start = _valid_time(h, m)
            rest = rest[: tm.start()] + " " + rest[tm.end():]
    dur = _DURATION.search(rest)
    if dur:
        value = float(dur.group(1).replace(",", "."))
        unit = dur.group(2).lower()
        p.minutes = int(round(value * (1 if unit.startswith("min") else 60)))
    else:
        for pattern, minutes in _DURATION_WORDS:
            if pattern.search(rest):
                p.minutes = minutes
                break
    p.booking = bool(_BOOK.search(text))
    # "bei Laura" / "zustaendig ist Laura" nennt die Zustaendige; "fuer X" kann Kunde oder
    # Mitarbeiter sein (wird in handle() gegen die Mitarbeiterliste geprueft).
    by = _ASSIGNEE_BEI.search(text)
    if by and by.group(1).lower() not in _PRONOUNS:
        # Das Muster ignoriert Gross-/Kleinschreibung: "bei Laura noch frei" -> nur "Laura".
        words = by.group(1).split()
        p.assignee_name = " ".join([words[0], *[w for w in words[1:] if w[:1].isupper()]])
    for_match = _FOR.search(text)
    if for_match and for_match.group(1).lower() not in _PRONOUNS:
        p.for_name = " ".join(w for w in for_match.group(1).split() if w.lower() not in WEEKDAYS) or None
    customer = _CUSTOMER.search(text)
    if customer:
        p.customer_name = customer.group(1).strip()
        # "...mit Max Mustermann Donnerstag": Wochentage/Zeitwoerter gehoeren nicht zum Namen.
        words = [w for w in p.customer_name.split() if w.lower() not in WEEKDAYS and w.lower() not in ("heute", "morgen", "übermorgen")]
        p.customer_name = " ".join(words) or None
    for pattern, kind in _KINDS:
        if pattern.search(text):
            p.kind = kind
            break
    topic = _TOPIC.search(text)
    if topic:
        p.topic = topic.group(1).strip()[:120]
    return p


def _members_matching(name: str, members) -> list:
    key = name.lower()
    out = []
    for member in members:
        full = cal.member_name(member).lower()
        local = member.email.split("@")[0].lower()
        if key == full or key == full.split()[0] or key == local or key == local.split(".")[0]:
            out.append(member)
    return out


def _customers_matching(name: str) -> list[Customer]:
    from app.services.customer_notes import _by_name, search_by_name_fragment

    found = _by_name(name)
    return found if found else search_by_name_fragment(name)


def _form_url(p: Parsed, assignee_id=None, customer=None, title=None) -> str:
    params = {}
    if p.day and p.start:
        start = f"{p.day.isoformat()}T{p.start.strftime('%H:%M')}"
        params["start"] = start
    if title:
        params["titel"] = title
    if assignee_id:
        params["mitarbeiter"] = assignee_id
    if customer is not None:
        params["kunde"], params["kunde_name"] = customer.id, customer.name
    return url_for("calendar.index") + ("?" + urlencode(params) if params else "")


def _when(event) -> str:
    start, end = to_local(event.starts_at), to_local(event.ends_at)
    days = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]
    return f"{days[start.weekday()]}, {start.strftime('%d.%m.')} {start.strftime('%H:%M')}–{end.strftime('%H:%M')} Uhr"


def _list_answer(user, text: str) -> dict:
    today = local_today()
    rel = _REL_DAY.search(text)
    if rel:
        day = today + timedelta(days={"heute": 0, "morgen": 1}.get(rel.group(1).lower(), 2))
        start_utc, end_utc = cal.local_day_bounds(day, day)
        label = {"heute": "Heute", "morgen": "Morgen"}.get(rel.group(1).lower(), "Übermorgen")
    else:
        start_utc, end_utc = cal.local_day_bounds(today, today + timedelta(days=6))
        label = "In den nächsten 7 Tagen"
    members = cal.office_members()
    assignee_ids = None
    if re.search(r"\b(ich|meine|mir)\b", text, re.I):
        assignee_ids = [user.id]
    else:
        who = re.search(r"\bhat\s+(\w+)", text, re.I)
        if who:
            found = _members_matching(who.group(1), members)
            if len(found) == 1:
                assignee_ids = [found[0].id]
    events = cal.events_between(start_utc, end_utc, assignee_ids, include_cancelled=False)
    lines = [f"{_when(e)} · {e.title} · {cal.member_name(e.assignee)}" + (f" · {e.customer.name}" if e.customer else "") for e in events[:12]]
    message = f"{label}: {len(events)} Termin(e)." if events else f"{label} sind keine Termine eingetragen."
    return {"kind": "calendar_list", "message": message, "lines": lines, "calendar_url": url_for("calendar.index")}


def handle(user, raw_text: str, choice: dict | None = None, client_ref: str | None = None, selected_customer_id=None) -> dict:
    """Ergebnis "kind": calendar_saved | calendar_question | calendar_list."""
    text = " ".join((raw_text or "").split())
    choice = choice if isinstance(choice, dict) else {}
    if _LIST.search(text) and not choice:
        return _list_answer(user, text)
    today = local_today()
    p = parse(text, today)
    members = cal.office_members()
    short = is_booking_command(text)
    if is_availability_question(text) and not choice:
        return _availability_answer(user, p, members, text)
    p.booking = p.booking or short
    # Auswahl aus einer Rueckfrage (freie Alternative, Dauer) ueberschreibt die erkannten Werte.
    if isinstance(choice.get("start"), str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}", choice["start"]):
        p.day, p.start, p.end = date.fromisoformat(choice["start"][:10]), time(int(choice["start"][11:13]), int(choice["start"][14:16])), None
    if str(choice.get("minutes") or "").isdigit():
        p.minutes, p.end = int(choice["minutes"]), None
    if p.booking and not p.customer_name and not p.for_name and not choice.get("customer_id"):
        p.customer_name = bare_name(text)

    if p.for_name:
        if _members_matching(p.for_name, members) and not p.assignee_name:
            p.assignee_name = p.for_name
        elif not p.customer_name:
            p.customer_name = p.for_name

    # Zustaendiger Mitarbeiter
    assignee = None
    defaults = []
    if choice.get("assignee_id"):
        assignee = next((m for m in members if m.id == int(choice["assignee_id"])), None)
    elif p.assignee_name:
        found = _members_matching(p.assignee_name, members)
        if len(found) == 1:
            assignee = found[0]
        elif found:
            return _question(p, f"Mehrere Mitarbeiter heißen „{p.assignee_name}“. Wer ist zuständig?",
                             [{"label": cal.member_name(m), "choice": {**choice, "assignee_id": m.id}} for m in found])
        else:
            return _question(p, f"„{p.assignee_name}“ ist kein Mitarbeiter Ihres Büros. Wer ist zuständig?",
                             [{"label": cal.member_name(m), "choice": {**choice, "assignee_id": m.id}} for m in members])
    if assignee is None and p.customer_name and not choice.get("customer_id"):
        # "Termin mit Laura": ein Mitarbeitername nach "mit" ist kein Kunde, sondern die Zustaendige.
        if len(_members_matching(p.customer_name, members)) == 1 and not _customers_matching(p.customer_name):
            assignee = _members_matching(p.customer_name, members)[0]
            p.customer_name = None
    assignee_id = assignee.id if assignee else None

    # Kunde
    customer = None
    if choice.get("customer_id"):
        customer = Customer.query.filter(Customer.id == int(choice["customer_id"])).first()
    elif choice.get("no_customer"):
        customer = None
    elif p.customer_name:
        found = _customers_matching(p.customer_name)
        if len(found) == 1:
            customer = found[0]
        elif found:
            options = [{"label": c.name + (f" · {c.city}" if c.city else ""), "choice": {**choice, "customer_id": c.id}} for c in found[:6]]
            options.append({"label": "Ohne Kundenverknüpfung", "choice": {**choice, "no_customer": True}})
            return _question(p, f"Mehrere Kunden passen zu „{p.customer_name}“{_when_text(p)}. Welcher ist gemeint?", options, assignee_id=assignee_id)
        else:
            return _question(
                p, f"Ich finde keinen Kunden „{p.customer_name}“. Termin ohne Kundenverknüpfung speichern?",
                [{"label": "Ohne Verknüpfung speichern", "choice": {**choice, "no_customer": True}}], assignee_id=assignee_id,
            )
    elif selected_customer_id not in (None, ""):
        customer = Customer.query.filter(Customer.id == int(selected_customer_id)).first()
    elif short and not choice.get("no_customer"):
        return _question(p, f"Für welchen Kunden ist der Termin{_when_text(p)}? Bitte den Namen ergänzen, z. B. „09.11.26 Mustermann Termin um 16.30“.",
                         [{"label": "Ohne Kunde buchen", "choice": {**choice, "no_customer": True}}])

    # Zustaendig: ausdruecklich genannt, sonst laut Kundendaten (Betreuer/Vermittlernummer). Beim
    # kurzen Buchungsauftrag wird ohne hinterlegte Zustaendigkeit gezielt nachgefragt; sonst wie
    # bisher die eingebende Person.
    if assignee is None:
        assignee = _responsible_for(customer, members)
        if assignee is not None:
            defaults.append("Zuständigkeit laut Kundendaten")
    if assignee is None and short:
        keep = {**choice, **({"customer_id": customer.id} if customer else {"no_customer": True})}
        who = f" für {customer.name}" if customer else ""
        return _question(p, f"Für den Termin{who}{_when_text(p)} ist kein zuständiger Mitarbeiter hinterlegt. Wer übernimmt ihn?",
                         [{"label": cal.member_name(m), "choice": {**keep, "assignee_id": m.id}} for m in members], customer=customer)
    if assignee is None:
        assignee = next((m for m in members if m.id == user.id), None)
        defaults.append("zuständig: Sie selbst")
    if assignee is None:
        return _question(p, "Wer ist für den Termin zuständig?", [{"label": cal.member_name(m), "choice": {**choice, "assignee_id": m.id}} for m in members])

    # Datum und Uhrzeit (Pflicht)
    title = f"Termin {customer.name}" if customer else (f"Termin mit {p.customer_name}" if p.customer_name else "Termin")
    if p.topic:
        title = f"{title} – {p.topic}"
    missing = [label for label, value in (("das Datum", p.day), ("die Uhrzeit", p.start)) if value is None]
    if missing:
        return _question(p, f"Für den Termin fehlt noch {' und '.join(missing)}. Bitte ergänzen, z. B. „Donnerstag um 14 Uhr“.", [],
                         assignee_id=assignee.id, customer=customer, title=title)
    minutes = p.minutes
    if p.end is not None:
        minutes = (p.end.hour * 60 + p.end.minute) - (p.start.hour * 60 + p.start.minute)
        if minutes <= 0:
            return _question(p, "Das Ende liegt vor dem Beginn. Bitte die Uhrzeit prüfen.", [], assignee_id=assignee.id, customer=customer, title=title)
    if minutes is None:
        office_minutes = _office_default_minutes()
        if office_minutes is None and short:
            keep = {**choice, "assignee_id": assignee.id, **({"customer_id": customer.id} if customer else {"no_customer": True})}
            return _question(p, f"Wie lange soll der Termin{_when_text(p)} dauern? (Im Büro ist keine Standarddauer hinterlegt.)",
                             [{"label": f"{m} Min.", "choice": {**keep, "minutes": m}} for m in (15, 30, 45, 60, 90)],
                             assignee_id=assignee.id, customer=customer, title=title)
        minutes = office_minutes or DEFAULT_MINUTES
        defaults.append(f"Dauer {minutes} Minuten" + (" (Büro-Vorgabe)" if office_minutes else ""))
    if not 5 <= minutes <= 12 * 60:
        return _question(p, "Die Dauer ist unplausibel. Bitte prüfen.", [], assignee_id=assignee.id, customer=customer, title=title)
    start_iso = f"{p.day.isoformat()}T{p.start.strftime('%H:%M')}"
    try:
        start_utc = cal.parse_local(start_iso, "den Beginn")
    except cal.CalendarError as exc:
        return _question(p, str(exc), [], assignee_id=assignee.id, customer=customer, title=title)
    if start_utc < utcnow_naive() and not choice.get("past_ok"):
        return _question(p, f"{p.day.strftime('%d.%m.%Y')} {p.start.strftime('%H:%M')} Uhr liegt in der Vergangenheit. Trotzdem eintragen?",
                         [{"label": "Trotzdem eintragen", "choice": {**choice, "past_ok": True, "assignee_id": assignee.id, **({"customer_id": customer.id} if customer else {"no_customer": True})}}],
                         assignee_id=assignee.id, customer=customer, title=title)
    end_iso = cal.local_iso(cal.parse_local(start_iso, "den Beginn") + timedelta(minutes=minutes))
    if p.booking:
        return _book(user, p, assignee, customer, title, start_iso, end_iso, start_utc, minutes, client_ref, defaults, choice)
    try:
        event, created, conflicts = cal.create_event(
            user,
            {
                "title": title, "start": start_iso, "end": end_iso, "assignee_id": assignee.id,
                "customer_id": customer.id if customer else None, "location_kind": p.kind or "",
                "note": None,
            },
            client_ref=client_ref,
        )
    except cal.CalendarError as exc:
        return _question(p, str(exc), [], assignee_id=assignee.id, customer=customer, title=title)
    parts = [f"Termin gespeichert: {_when(event)}", cal.member_name(event.assignee)]
    if event.customer:
        parts.append(f"Kunde {event.customer.name}")
    message = ", ".join(parts) + "."
    if defaults:
        message += " Angenommen: " + ", ".join(defaults) + "."
    if conflicts:
        message += " Achtung: überschneidet sich mit " + ", ".join(f"„{c.title}“ ({to_local(c.starts_at).strftime('%H:%M')})" for c in conflicts) + "."
    return {
        "kind": "calendar_saved",
        "duplicate": not created,
        "message": message if created else "Dieser Termin wurde bereits gespeichert.",
        "event": cal.serialize(event, user),
        "calendar_url": url_for("calendar.index"),
        "tone": "warn" if conflicts else "ok",
    }


def _question(p: Parsed, message: str, options: list, assignee_id=None, customer=None, title=None) -> dict:
    return {
        "kind": "calendar_question",
        "message": message,
        "options": options,
        "form_url": _form_url(p, assignee_id=assignee_id, customer=customer, title=title),
    }



def _book(user, p: Parsed, assignee, customer, title, start_iso, end_iso, start_utc, minutes, client_ref, defaults, choice=None) -> dict:
    """Ausdrueckliche Buchung (app/services/booking.py): erneute Pruefung unmittelbar vor dem
    Speichern; belegt -> nicht buchen, freie Alternativen nennen."""
    from app.services import booking

    try:
        result = booking.book(user, assignee=assignee, start_utc=start_utc, minutes=minutes, customer=customer, title=title,
                              location_kind=p.kind or "", client_ref=client_ref)
    except cal.CalendarError as exc:
        return _question(p, str(exc), [], assignee_id=assignee.id, customer=customer, title=title)
    if result.reason:
        message = f"Nicht gebucht: {result.reason}"
        if result.alternatives:
            message += " Freie Alternativen bei " + cal.member_name(assignee) + ": " + "; ".join(a.label() for a in result.alternatives) + "."
        else:
            message += " Keine freie Alternative im Kalender gefunden."
        keep = {**(choice or {}), "assignee_id": assignee.id, "minutes": minutes,
                **({"customer_id": customer.id} if customer else {"no_customer": True})}
        options = [{"label": "Buchen: " + a.label(), "choice": {**keep, "start": a.iso()}} for a in result.alternatives]
        return {"kind": "calendar_question", "message": message, "options": options,
                "form_url": _form_url(p, assignee_id=assignee.id, customer=customer, title=title)}
    event = result.event
    if not result.created:
        return {"kind": "calendar_saved", "duplicate": True, "message": f"Dieser Termin ist bereits gebucht: {_when(event)}, {cal.member_name(event.assignee)}.",
                "event": cal.serialize(event, user), "calendar_url": url_for("calendar.index"), "tone": "ok"}
    local = to_local(event.starts_at)
    who = event.customer.name if event.customer else (p.customer_name or "ohne Kunde")
    message = (f"Termin gebucht (bestätigt): {who} am {local.strftime('%d.%m.%Y')} um {local.strftime('%H:%M')} Uhr "
               f"({minutes} Min.), zuständig: {cal.member_name(event.assignee)}.")
    if defaults:
        message += " Angenommen: " + ", ".join(defaults) + "."
    return {"kind": "calendar_saved", "duplicate": False, "message": message, "event": cal.serialize(event, user),
            "calendar_url": url_for("calendar.index"), "tone": "ok"}


def _when_text(p: Parsed) -> str:
    if p.day and p.start:
        return f" am {p.day.strftime('%d.%m.%Y')} um {p.start.strftime('%H:%M')} Uhr"
    return ""


def _availability_answer(user, p: Parsed, members, text: str) -> dict:
    """Reine Verfuegbarkeitsfrage: pruefen und antworten, nichts speichern."""
    from app.services import availability

    assignee = None
    if p.assignee_name:
        found = _members_matching(p.assignee_name, members)
        assignee = found[0] if len(found) == 1 else None
    if assignee is None:
        assignee = next((m for m in members if m.id == user.id), None)
    minutes = p.minutes or _office_default_minutes() or DEFAULT_MINUTES
    start_utc = cal.parse_local(f"{p.day.isoformat()}T{p.start.strftime('%H:%M')}", "den Beginn")
    when = f"{p.day.strftime('%d.%m.%Y')} um {p.start.strftime('%H:%M')} Uhr"
    s = availability.settings()
    lines = []
    if availability.planning_ready(s):
        reason = availability.check(assignee, start_utc, minutes, s)
        if reason:
            from app.services.booking import alternatives_near

            lines = [a.label() for a in alternatives_near(assignee, start_utc, minutes, s)]
    else:
        busy = cal.conflicts_for(assignee.id, start_utc, start_utc + timedelta(minutes=minutes))
        reason = f"{cal.member_name(assignee)} hat dann bereits „{busy[0].title}“." if busy else None
    name = cal.member_name(assignee)
    if reason:
        message = f"Am {when} ist bei {name} nicht frei: {reason}"
    else:
        message = f"Am {when} ist bei {name} frei ({minutes} Min.)."
    if not availability.planning_ready(s):
        message += " (Bürozeiten sind nicht hinterlegt – geprüft wurden nur vorhandene Termine.)"
    message += " Es wurde nichts gebucht." + (" Freie Alternativen:" if lines else "")
    return {"kind": "calendar_list", "message": message, "lines": lines, "calendar_url": url_for("calendar.index")}
