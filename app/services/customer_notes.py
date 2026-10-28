"""Kundennotizen und offene Rueckrufe ueber das ZentriqAI-Eingabefeld.

Ablauf einer Eingabe (handle_input):
1. Kundenbezug erkennen - deterministisch, ohne KI, ausschliesslich im eigenen Buero (globaler
   Tenant-Filter, app/tenancy.py):
   a) Versicherungsnehmernummer ("808/123456-E"): Kundennummer des Kunden bzw. die
      Vorgangsnummern der Leipziger Liste, die dem Kunden beim Import zugeordnet wurden
      ("808/123456-E" und "808/123456-E-12" gehoeren zur selben VN-Nummer).
   b) Telefonnummer in jeder ueblichen Schreibweise (normalisierter phone_key).
   c) Vollstaendiger Name eines vorhandenen Kunden (alle Namensbestandteile im Text).
   d) Ohne Kennung: der in der Oberflaeche sichtbar ausgewaehlte Kunde (customer_id der Anfrage).
   Mehrere Treffer -> Auswahl, kein Treffer -> Hinweis. Es wird nie ein Kunde angelegt oder
   geaendert. Nennt der Text neben der Nummer einen anderen vorhandenen Kunden -> Rueckfrage.
2. Absicht: Fragen sowie reine Namen/Nummern -> Suche (Kundenkarte, nichts wird gespeichert).
   Bleibt nach Entfernen der Kennungen Text uebrig -> Notiz. Ohne Kundenbezug geht die Eingabe
   unveraendert an den KI-Chat ("chat"). Unklare Faelle -> kurze Rueckfrage.
3. Notiz speichern: Text bleibt inhaltlich erhalten (nur Kennungen entfernt, Schreibweisen wie
   "huk 24" -> "HUK24" vereinheitlicht). Nur ausdrueckliche Formulierungen ("nicht erreicht",
   "bitte zurueckrufen", "ruft ... zurueck") legen einen Kontaktvorgang an bzw. ergaenzen den
   offenen Vorgang zum selben Anlass. Doppelte Uebermittlung (gleiche client_ref oder gleicher
   Text innerhalb weniger Sekunden) speichert nur einmal.

Das KI-Modell wird hierfuer nicht benoetigt: Zuordnung, Speicherung und Antworten auf Fragen
stuetzen sich ausschliesslich auf die Datenbank - schnell, kostenlos und ohne dass Kundendaten
den Server verlassen."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import timedelta

from flask import url_for
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.extensions import db
from app.models import Customer, CustomerContact, CustomerNote, Document, LeipzigerEntry, User
from app.models.enums import DocStatus, DocType
from app.services import customer_sources
from app.services.customer_lock import CustomerLockTimeout, customer_creation_lock, fresh_transaction
from app.services.memo_customer_match import (
    _PHONE_PATTERN,
    _by_name,
    extract_customer_numbers,
    extract_phone_numbers,
)
from app.services.timetracking.clock import local_today, to_local, utcnow_naive
from app.utils.customer_keys import name_key, name_parts, normalize_phone
from app.utils.vermittlernummer import vermittlernummer_key

MAX_INPUT_CHARS = 1000
MAX_CANDIDATES = 5
HISTORY_LIMIT = 30
CALLBACK_LIMIT = 30
# Gleicher Text beim selben Kunden vom selben Benutzer innerhalb dieser Zeit = Doppelversand.
DUPLICATE_WINDOW_SECONDS = 30
# Ein Kontaktstand ohne Anlass uebernimmt den Anlass der letzten normalen Notiz dieses Zeitraums.
RECENT_TOPIC_DAYS = 7

OPEN, DONE = "open", "done"
NOT_REACHED, CALLBACK_REQUESTED, CUSTOMER_CALLS_BACK = "not_reached", "callback_requested", "customer_calls_back"
OUTCOME_LABELS = {
    NOT_REACHED: "Nicht erreicht",
    CALLBACK_REQUESTED: "Rückruf erbeten",
    CUSTOMER_CALLS_BACK: "Kunde ruft zurück",
    DONE: "Erledigt",
}

# Versicherungsnehmernummer im HUK-Format der Leipziger Liste: 808/123456-E (optional -12).
# Gueltige Schreibweisen derselben Nummer: "808/123456-E", "808123456e", "808 123456 E",
# "808-123456-e", "808/123456e" - optional mit Vertragsendung "-12". Fuehrende Nullen und der
# Buchstabe bleiben erhalten; Abgleich immer ueber denselben Schluessel "808123456E"
# (normalize_customer_number bzw. search_key - wie Kundensuche, Dubletten- und Listenabgleich).
_VN_PATTERN = re.compile(
    r"(?<![\w/])(\d{3})(?:\s*[/-]\s*|\s+)?(\d{6})(?:\s*-\s*|\s+)?([A-Za-z])(?:\s*-\s*(\d{2}))?(?!\w)"
)
VN_KEY_LENGTH = 10  # "808123456E"
_VN_DISPLAY = re.compile(r"^(\d{3}/\d{6}-[A-Z])")

# Ausdrueckliche Kontaktstaende. Reihenfolge = Vorrang ("nicht erreicht, ruft zurueck" -> ruft zurueck).
_U = "(?:ü|ue)"
_OUTCOME_PATTERNS = (
    (CUSTOMER_CALLS_BACK, re.compile(rf"\b(?:ruft|rufen)\b[^.;!?]{{0,40}}?\bzur{_U}ck\b|\bmeldet\s+sich\b(?:[^.;!?]{{0,30}}?\bzur{_U}ck\b)?", re.I)),
    (
        CALLBACK_REQUESTED,
        re.compile(
            rf"\bbitte\s+(?:um\s+)?(?:zur{_U}ckrufen|r{_U}ckruf)\b|\bbittet\s+um\s+r{_U}ckruf\b|"
            rf"\br{_U}ckruf\s+(?:erbeten|gew{_U}nscht|erforderlich|n(?:ö|oe)tig)\b|\bzur{_U}ckrufen\b",
            re.I,
        ),
    ),
    (
        NOT_REACHED,
        re.compile(
            r"\bnicht\s+(?:erreicht|erreichbar|angenommen|rangegangen|abgenommen|abgehoben)\b|\bniemanden?\s+erreicht\b|"
            r"\bmailbox\b|\banrufbeantworter\b|\bkeine?\s+(?:antwort|anschluss)\b",
            re.I,
        ),
    ),
)
# Fragen bzw. Suchauftraege.
_QUESTION_START = re.compile(
    r"^\s*(?:warum|wieso|weshalb|worum|was|wer|wann|wo|wie|welche[rsmn]?|gibt\s+es|hat|haben|ist|sind|zeig\w*|such\w*|finde?)\b", re.I
)
# Arbeitsauftraege an die KI (Text schreiben, erklaeren ...) - nie als Notiz speichern.
_CHAT_REQUEST = re.compile(
    r"\b(?:formulier|schreib|fass|zusammenfass|erkl(?:ä|ae)r|erstell|(?:ü|ue)bersetz|analysier|vergleich|entw(?:u|ü|ue)rf|entwirf|verfass)\w*",
    re.I,
)
_OPEN_QUESTION = re.compile(r"\boffen\w*|\bausstehend\w*|\bnoch\s+zu\s+tun\b|\br(?:ü|ue)ckruf\w*", re.I)
_REASON_QUESTION = re.compile(r"\b(?:warum|wieso|weshalb|worum|anlass|grund)\b", re.I)
# Behutsame Vereinheitlichung gaengiger Schreibweisen (Inhalt bleibt unveraendert).
_SPELLING = (
    (re.compile(r"\bhuk\s*-?\s*24\b", re.I), "HUK24"),
    (re.compile(r"\bhuk\b", re.I), "HUK"),
    (re.compile(r"\bkfz\b", re.I), "Kfz"),
    # Gaengige Fachbegriffe gross ("huk24 wechsel" -> "HUK24 Wechsel"); Inhalt bleibt gleich.
    (
        re.compile(
            r"\b(wechsel|termin|angebot|vertrag|k(?:ü|ue)ndigung|schaden|unterlagen|beitrag|tarif|hausrat|haftpflicht|"
            r"rechtsschutz|versicherung|r(?:ü|ue)ckruf)\b"
        ),
        lambda m: m.group(1)[:1].upper() + m.group(1)[1:],
    ),
)
# Woerter, die am Rand eines Anlasses nichts zum Anlass beitragen.
_TOPIC_FILLER = frozenset(
    {
        "wegen", "wg", "angerufen", "anruf", "angeschrieben", "kunde", "kundin", "heute", "telefonat", "telefoniert",
        "bzgl", "bezueglich", "bezüglich", "betreffend", "betr", "zu", "zum", "zur", "re", "und", "aber", "leider",
        "erneut", "nochmal", "nochmals", "wieder", "gerade", "der", "die", "das", "den", "ist", "war", "hat", "wurde",
        "morgen", "später", "spaeter", "versucht", "versuch", "kontaktversuch", "herr", "frau",
    }
)
_SALUTATION = frozenset({"herr", "herrn", "frau", "kunde", "kundin", "bei", "fuer", "für", "von", "mit"})


class NoteError(ValueError):
    """Fehler mit einer fuer den Nutzer verstaendlichen Meldung."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


# --- Erkennung ------------------------------------------------------------------------------


@dataclass
class Parsed:
    text: str
    vn_keys: list[str] = field(default_factory=list)
    vn_spans: list[tuple[int, int]] = field(default_factory=list)
    phones: list[str] = field(default_factory=list)
    numbers: list[str] = field(default_factory=list)

    @property
    def has_identifier(self) -> bool:
        return bool(self.vn_keys or self.phones or self.numbers)


def parse(text: str) -> Parsed:
    parsed = Parsed(text=text)
    for match in _VN_PATTERN.finditer(text):
        key = "".join(part for part in match.groups() if part).upper()
        if key not in parsed.vn_keys:
            parsed.vn_keys.append(key)
        parsed.vn_spans.append(match.span())
    rest = _remove_spans(text, parsed.vn_spans)
    parsed.phones = extract_phone_numbers(rest)
    parsed.numbers = [n for n in extract_customer_numbers(rest) if n not in parsed.vn_keys]
    return parsed


def _remove_spans(text: str, spans: list[tuple[int, int]]) -> str:
    for start, end in sorted(spans, reverse=True):
        text = text[:start] + " " + text[end:]
    return text


def detect_outcome(text: str) -> tuple[str | None, list[tuple[int, int]]]:
    for outcome, pattern in _OUTCOME_PATTERNS:
        spans = [m.span() for m in pattern.finditer(text)]
        if spans:
            # Weitere (schwaechere) Kontaktstaende im selben Text gehoeren ebenfalls nicht zum Anlass.
            for _other, other_pattern in _OUTCOME_PATTERNS:
                spans += [m.span() for m in other_pattern.finditer(text)]
            return outcome, spans
    return None, []


def is_question(text: str) -> bool:
    return text.rstrip().endswith("?") or bool(_QUESTION_START.match(text))


def customers_by_vn(key: str) -> list[int]:
    """Kunden zu einer VN-Nummer: Kundennummer oder zugeordnete Vorgaenge der Leipziger Liste
    (alle Listen des Bueros), deren Vorgangsnummer genau diese Nummer ist bzw. mit ihr beginnt
    und nur noch eine Ziffernendung hat ("808123456E" -> "808123456E12")."""
    # Kundennummer = VN-Nummer; eine Vertragsnummer ("...-E-12") gehoert zur VN "...-E".
    number_keys = {key, key[:VN_KEY_LENGTH]} if len(key) > VN_KEY_LENGTH else {key}
    ids = set(db.session.execute(select(Customer.id).where(Customer.customer_number_key.in_(number_keys))).scalars())
    rows = db.session.execute(
        select(LeipzigerEntry.customer_id, LeipzigerEntry.contract_key).where(
            LeipzigerEntry.customer_id.is_not(None), LeipzigerEntry.contract_key.like(f"{key}%")
        )
    ).all()
    for customer_id, contract_key in rows:
        rest = (contract_key or "")[len(key):]
        if rest == "" or rest.isdigit():
            ids.add(customer_id)
    return sorted(ids)


def _load_customers(ids) -> list[Customer]:
    ids = list(ids)[: MAX_CANDIDATES + 1]
    if not ids:
        return []
    return Customer.query.filter(Customer.id.in_(ids)).order_by(Customer.name, Customer.id).all()


def find_by_identifiers(parsed: Parsed) -> tuple[str | None, list[Customer]]:
    """(Grundlage, Kandidaten) ueber VN-Nummer, Kundennummer oder Telefonnummer - in dieser Reihenfolge."""
    if parsed.vn_keys:
        ids: set[int] = set()
        for key in parsed.vn_keys:
            ids.update(customers_by_vn(key))
        return "vn", _load_customers(ids)
    if parsed.numbers:
        rows = db.session.execute(select(Customer.id).where(Customer.customer_number_key.in_(parsed.numbers))).scalars()
        return "customer_number", _load_customers(set(rows))
    if parsed.phones:
        rows = db.session.execute(select(Customer.id).where(Customer.phone_key.in_(parsed.phones))).scalars()
        return "phone", _load_customers(set(rows))
    return None, []


def search_by_name_fragment(text: str) -> list[Customer]:
    """Teilname ("Heller"): Kunden, deren Name alle eingegebenen Woerter enthaelt - nur als
    Auswahl, nie als automatische Zuordnung."""
    words = name_parts(text)
    if not words or len(words) > 3 or any(len(word) < 3 for word in words):
        return []
    query = Customer.query.filter(Customer.name_key.isnot(None))
    for word in words:
        query = query.filter(Customer.name_key.like(f"%{word}%"))
    rows = query.order_by(Customer.name, Customer.id).limit(MAX_CANDIDATES + 1).all()
    return [c for c in rows if all(word in (c.name_key or "").split() for word in words)]


# --- Text ---------------------------------------------------------------------------------


def _tokens(text: str) -> list[tuple[int, int, list[str]]]:
    return [(m.start(), m.end(), name_parts(m.group(0))) for m in re.finditer(r"\S+", text)]


def find_name_span(text: str, customer: Customer) -> tuple[int, int] | None:
    """Stelle, an der der vollstaendige Name des Kunden zusammenhaengend im Text steht
    (Reihenfolge egal, inkl. vorangestelltem "Herr"/"Frau")."""
    needed = set((customer.name_key or "").split())
    if len(needed) < 1:
        return None
    tokens = _tokens(text)
    for i, (start, _end, parts) in enumerate(tokens):
        if not parts or not set(parts) <= needed:
            continue
        found: set[str] = set()
        for j in range(i, len(tokens)):
            if not tokens[j][2] or not set(tokens[j][2]) <= needed:
                break
            found.update(tokens[j][2])
            if found == needed:
                begin = start
                if i > 0 and tokens[i - 1][2] and set(tokens[i - 1][2]) <= _SALUTATION:
                    begin = tokens[i - 1][0]
                return begin, tokens[j][1]
    return None


def _clean(text: str) -> str:
    text = " ".join(text.split())
    text = re.sub(r"\s+([,.;:!?])", r"\1", text)
    text = re.sub(r"([,;:])(?:\s*[,;:])+", r"\1", text)
    return text.strip(" ,;:-–—/·")


def normalize_note(text: str) -> str:
    for pattern, replacement in _SPELLING:
        text = pattern.sub(replacement, text)
    text = _clean(text)
    return text[:1].upper() + text[1:] if text else ""


def topic_from(note_text: str) -> str | None:
    """Kurzer Anlass: Notiz ohne Kontaktstand und ohne Fuellwoerter am Rand."""
    _outcome, spans = detect_outcome(note_text)
    text = _clean(_remove_spans(note_text, spans))
    words = text.split()
    while words and words[0].strip(",.;:").lower() in _TOPIC_FILLER:
        words.pop(0)
    while words and words[-1].strip(",.;:").lower() in _TOPIC_FILLER:
        words.pop()
    topic = _clean(" ".join(words))
    return (topic[:1].upper() + topic[1:])[:200] if topic else None


def _topic_key(topic: str | None) -> frozenset[str]:
    return frozenset(w for w in name_parts(re.sub(r"(?<=[a-zA-Z])(?=\d)", " ", topic or "")) if w not in _TOPIC_FILLER)


def same_topic(left: str | None, right: str | None) -> bool:
    a, b = _topic_key(left), _topic_key(right)
    return bool(a and b) and (a <= b or b <= a)


# --- Darstellung ----------------------------------------------------------------------------


def user_name(user: User | None) -> str:
    if user is None:
        return "Unbekannt"
    profile = user.employee_profile
    if profile is not None and (profile.display_name or "").strip():
        return profile.display_name.strip()
    return user.email


def when(value) -> str:
    local = to_local(value)
    if local is None:
        return ""
    today = local_today()
    if local.date() == today:
        day = "heute"
    elif local.date() == today - timedelta(days=1):
        day = "gestern"
    else:
        day = local.strftime("%d.%m.%Y")
    return f"{day}, {local.strftime('%H:%M')} Uhr"


def vn_numbers(customer: Customer) -> list[str]:
    """Anzeige der VN-Nummer(n): Kundennummer, sonst aus den zugeordneten Vorgaengen."""
    if customer.customer_number:
        return [customer.customer_number]
    numbers: list[str] = []
    rows = db.session.execute(
        select(LeipzigerEntry.contract_number)
        .where(LeipzigerEntry.customer_id == customer.id, LeipzigerEntry.contract_number.is_not(None))
        .order_by(LeipzigerEntry.id.desc())
        .limit(200)
    ).scalars()
    for number in rows:
        match = _VN_DISPLAY.match(number.strip().upper())
        if match and match.group(1) not in numbers:
            numbers.append(match.group(1))
    return numbers[:3]


def serialize_candidate(customer: Customer) -> dict:
    place = " ".join(filter(None, [customer.postal_code, customer.city])) or None
    return {
        "id": customer.id,
        "name": customer.name,
        "vn": ", ".join(vn_numbers(customer)) or None,
        "phone": customer.phone or None,
        "place": place,
        "birth": customer.date_of_birth.strftime("%d.%m.%Y") if customer.date_of_birth else None,
    }


def serialize_note(note: CustomerNote) -> dict:
    return {
        "id": note.id,
        "text": note.text,
        "event": note.event,
        "event_label": OUTCOME_LABELS.get(note.event),
        "author": user_name(note.created_by),
        "when": when(note.created_at),
        "contact_id": note.contact_id,
    }


def serialize_contact(contact: CustomerContact, with_customer: bool = False) -> dict:
    data = {
        "id": contact.id,
        "topic": contact.topic,
        "status": contact.status,
        "status_label": "Erledigt" if contact.status == DONE else OUTCOME_LABELS.get(contact.outcome, "Offen"),
        "attempts": contact.attempts,
        "by": user_name(contact.updated_by or contact.created_by),
        "created_by": user_name(contact.created_by),
        "when": when(contact.updated_at),
    }
    if with_customer:
        data["customer"] = {"id": contact.customer_id, "name": contact.customer.name if contact.customer else ""}
    return data


def current_list() -> Document | None:
    return db.session.execute(
        select(Document)
        .where(Document.doc_type == DocType.LEIPZIGER_LISTE, Document.status == DocStatus.DONE)
        .order_by(Document.uploaded_at.desc(), Document.id.desc())
        .limit(1)
    ).scalar_one_or_none()


def _entry_status(entry) -> str:
    code = (entry.status_code or "").upper()
    label = {"ANG": "Angebot", "NEU": "Neugeschäft", "FZW": "Fahrzeugwechsel", "STORNO": "Storno"}.get(code, code or "Vorgang")
    if entry.start_date:
        try:
            day = f"{entry.start_date[8:10]}.{entry.start_date[5:7]}.{entry.start_date[:4]}"
        except (TypeError, IndexError):
            day = entry.start_date
        return f"{label} · Beginn {day}"
    return f"{label} · ohne Beginn (offen)"


def leipziger_summary(user, customer: Customer) -> dict:
    """Vorgaenge der aktuellen Leipziger Liste, die beim Import diesem Kunden zugeordnet wurden.
    Mitarbeiter sehen wie in der Liste nur Vorgaenge ihrer eigenen Vermittlernummer.
    state: "found" | "none" | "no_list" | "error" - "none" und "error" werden unterschieden."""
    try:
        document = current_list()
        if document is None:
            return {"state": "no_list", "entries": [], "hidden": 0}
        rows = db.session.execute(
            select(LeipzigerEntry)
            .where(
                LeipzigerEntry.document_id == document.id,
                LeipzigerEntry.customer_id == customer.id,
                LeipzigerEntry.tenant_id == customer.tenant_id,
            )
            .order_by(LeipzigerEntry.position, LeipzigerEntry.id)
        ).scalars().all()
        own_key = vermittlernummer_key(user.vermittlernummer)
        visible = [e for e in rows if user.is_office_admin or (own_key and e.broker_key == own_key)]
        entries = [
            {
                "number": entry.contract_number or "ohne Nummer",
                "product": entry.product_line,
                "status": _entry_status(entry),
                "broker": entry.broker_number,
                "url": url_for(
                    "leipziger.index",
                    document_id=document.id,
                    q=entry.contract_number or customer.name,
                    tab="mit-datum" if entry.start_date else "ohne-datum",
                ),
            }
            for entry in visible[:10]
        ]
        return {
            "state": "found" if rows else "none",
            "entries": entries,
            "hidden": len(rows) - len(visible),
            "list": document.original_filename,
        }
    except Exception:  # noqa: BLE001 - Ladefehler wird als solcher angezeigt, nicht als "kein Eintrag".
        from flask import current_app

        current_app.logger.exception("customer_notes.leipziger_failed customer=%s", customer.id)
        db.session.rollback()
        return {"state": "error", "entries": [], "hidden": 0}


def customer_card(user, customer: Customer) -> dict:
    notes = (
        CustomerNote.query.filter(CustomerNote.customer_id == customer.id)
        .order_by(CustomerNote.created_at.desc(), CustomerNote.id.desc())
        .limit(HISTORY_LIMIT + 1)
        .all()
    )
    contacts = (
        CustomerContact.query.filter(CustomerContact.customer_id == customer.id, CustomerContact.status == OPEN)
        .order_by(CustomerContact.updated_at.desc(), CustomerContact.id.desc())
        .all()
    )
    latest = next((n for n in notes if n.event is None), notes[0] if notes else None)
    card = {
        **serialize_candidate(customer),
        "latest": serialize_note(latest) if latest else None,
        "open_contacts": [serialize_contact(c) for c in contacts],
        "notes": [serialize_note(n) for n in notes[:HISTORY_LIMIT]],
        "more_notes": len(notes) > HISTORY_LIMIT,
        "leipziger": leipziger_summary(user, customer),
        "customer_url": url_for("customers.detail", customer_id=customer.id) if user.is_office_admin else None,
    }
    return card


def open_callbacks(limit: int = CALLBACK_LIMIT) -> list[dict]:
    contacts = (
        CustomerContact.query.filter(CustomerContact.status == OPEN)
        .order_by(CustomerContact.updated_at.desc(), CustomerContact.id.desc())
        .limit(limit)
        .all()
    )
    return [serialize_contact(c, with_customer=True) for c in contacts]


# --- Speichern ------------------------------------------------------------------------------


def _existing_by_ref(user, client_ref: str | None) -> CustomerNote | None:
    if not client_ref:
        return None
    return CustomerNote.query.filter(CustomerNote.created_by_user_id == user.id, CustomerNote.client_ref == client_ref).first()


def _recent_duplicate(user, customer: Customer, text: str) -> CustomerNote | None:
    since = utcnow_naive() - timedelta(seconds=DUPLICATE_WINDOW_SECONDS)
    return (
        CustomerNote.query.filter(
            CustomerNote.customer_id == customer.id,
            CustomerNote.created_by_user_id == user.id,
            CustomerNote.text == text,
            CustomerNote.created_at >= since,
        )
        .order_by(CustomerNote.id.desc())
        .first()
    )


def _commit_note(note: CustomerNote, user) -> tuple[CustomerNote, bool]:
    """Speichert und committet. Bei paralleler Doppelanfrage (gleiche client_ref) gewinnt die erste."""
    db.session.add(note)
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        existing = _existing_by_ref(user, note.client_ref)
        if existing is None:
            raise
        return existing, False
    return note, True


def open_contacts_of(customer: Customer) -> list[CustomerContact]:
    return (
        CustomerContact.query.filter(CustomerContact.customer_id == customer.id, CustomerContact.status == OPEN)
        .order_by(CustomerContact.updated_at.desc(), CustomerContact.id.desc())
        .all()
    )


def _get_open_contact(customer: Customer, contact_id: int) -> CustomerContact:
    contact = CustomerContact.query.filter(
        CustomerContact.id == contact_id, CustomerContact.customer_id == customer.id
    ).first()
    if contact is None:
        raise NoteError("Der Vorgang wurde nicht gefunden.", 404)
    return contact


def choose_contact(customer: Customer, topic: str | None, contact_choice) -> tuple[str, CustomerContact | None]:
    """Welcher offene Vorgang wird ergaenzt? ("existing", Vorgang) | ("new", None) | ("ask", None)."""
    if contact_choice == "new":
        return "new", None
    if contact_choice not in (None, ""):
        try:
            contact = _get_open_contact(customer, int(contact_choice))
        except (TypeError, ValueError):
            raise NoteError("Ungültiger Vorgang.") from None
        if contact.status != OPEN:
            raise NoteError("Dieser Vorgang ist bereits erledigt.", 409)
        return "existing", contact
    contacts = open_contacts_of(customer)
    if topic:
        matching = [c for c in contacts if same_topic(c.topic, topic)]
        if len(matching) == 1:
            return "existing", matching[0]
        return ("ask", None) if matching else ("new", None)
    if not contacts:
        return "new", None
    return ("existing", contacts[0]) if len(contacts) == 1 else ("ask", None)


def _recent_topic(customer: Customer) -> str | None:
    since = utcnow_naive() - timedelta(days=RECENT_TOPIC_DAYS)
    latest = (
        CustomerNote.query.filter(
            CustomerNote.customer_id == customer.id, CustomerNote.event.is_(None), CustomerNote.created_at >= since
        )
        .order_by(CustomerNote.created_at.desc(), CustomerNote.id.desc())
        .first()
    )
    return topic_from(latest.text) if latest is not None else None


def save_note(user, customer: Customer, text: str, *, client_ref: str | None = None, contact_choice=None) -> dict:
    """Speichert eine Notiz (idempotent). Liefert das Ergebnis fuer die Oberflaeche."""
    if customer.tenant_id != user.tenant_id:
        raise NoteError("Der Kunde gehört nicht zu Ihrem Büro.", 404)
    text = text.strip()[:MAX_INPUT_CHARS]
    if not text:
        raise NoteError("Die Notiz ist leer.")
    existing = _existing_by_ref(user, client_ref) or _recent_duplicate(user, customer, text)
    if existing is not None:
        return _saved_result(user, existing, duplicate=True)

    outcome, _spans = detect_outcome(text)
    contact = None
    now = utcnow_naive()
    if outcome:
        topic = topic_from(text)
        decision, contact = choose_contact(customer, topic, contact_choice)
        if decision == "new" and topic is None and contact_choice in (None, ""):
            # "nicht erreicht" ohne Anlass bezieht sich auf das zuletzt dokumentierte Anliegen
            # (wie auf der Kundenkarte angezeigt) - es wird kein Anlass erfunden.
            topic = _recent_topic(customer)
        if decision == "ask":
            return {
                "kind": "choose_contact",
                "message": f"Zu welchem offenen Vorgang bei {customer.name} gehört die Notiz?",
                "customer": serialize_candidate(customer),
                "contacts": [serialize_contact(c) for c in open_contacts_of(customer)],
                "note": text,
            }
        if contact is None:
            contact = CustomerContact(
                tenant_id=customer.tenant_id,
                customer_id=customer.id,
                topic=topic,
                status=OPEN,
                outcome=outcome,
                attempts=1,
                created_by_user_id=user.id,
                created_at=now,
                updated_by_user_id=user.id,
                updated_at=now,
            )
            db.session.add(contact)
        else:
            contact.outcome = outcome
            contact.attempts = (contact.attempts or 0) + 1
            contact.updated_by_user_id = user.id
            contact.updated_at = now
            if not contact.topic and topic:
                contact.topic = topic
        db.session.flush()

    note = CustomerNote(
        tenant_id=customer.tenant_id,
        customer_id=customer.id,
        contact_id=contact.id if contact is not None else None,
        text=text,
        event=outcome,
        created_by_user_id=user.id,
        created_at=now,
        client_ref=client_ref or None,
    )
    note, created = _commit_note(note, user)
    return _saved_result(user, note, duplicate=not created)


def _saved_result(user, note: CustomerNote, duplicate: bool) -> dict:
    customer = note.customer
    return {
        "kind": "saved",
        "duplicate": duplicate,
        "message": f"Bei {customer.name} gespeichert: {note.text}",
        "note": serialize_note(note),
        "card": customer_card(user, customer),
    }


def mark_not_reached(user, customer: Customer, *, contact_id=None, note_id=None, client_ref: str | None = None) -> dict:
    """Schnellaktion "Nicht erreicht": ergaenzt den gewaehlten offenen Vorgang bzw. legt einen
    offenen Vorgang zum Anlass der gewaehlten Notiz an."""
    existing = _existing_by_ref(user, client_ref)
    if existing is not None:
        return _saved_result(user, existing, duplicate=True)
    now = utcnow_naive()
    if contact_id not in (None, ""):
        contact = _get_open_contact(customer, _int(contact_id))
        if contact.status != OPEN:
            raise NoteError("Dieser Vorgang ist bereits erledigt.", 409)
        contact.outcome = NOT_REACHED
        contact.attempts = (contact.attempts or 0) + 1
        contact.updated_by_user_id = user.id
        contact.updated_at = now
    else:
        topic = None
        if note_id not in (None, ""):
            source = CustomerNote.query.filter(CustomerNote.id == _int(note_id), CustomerNote.customer_id == customer.id).first()
            if source is None:
                raise NoteError("Die Notiz wurde nicht gefunden.", 404)
            topic = topic_from(source.text)
        decision, contact = choose_contact(customer, topic, None) if topic else ("new", None)
        if decision != "existing" or contact is None:
            contact = CustomerContact(
                tenant_id=customer.tenant_id, customer_id=customer.id, topic=topic, status=OPEN, outcome=NOT_REACHED,
                attempts=1, created_by_user_id=user.id, created_at=now, updated_by_user_id=user.id, updated_at=now,
            )
            db.session.add(contact)
        else:
            contact.outcome = NOT_REACHED
            contact.attempts = (contact.attempts or 0) + 1
            contact.updated_by_user_id = user.id
            contact.updated_at = now
    db.session.flush()
    text = "Nicht erreicht" + (f" ({contact.topic})" if contact.topic else "")
    note = CustomerNote(
        tenant_id=customer.tenant_id, customer_id=customer.id, contact_id=contact.id, text=text, event=NOT_REACHED,
        created_by_user_id=user.id, created_at=now, client_ref=client_ref or None,
    )
    note, created = _commit_note(note, user)
    return _saved_result(user, note, duplicate=not created)


def close_contact(user, contact: CustomerContact, *, client_ref: str | None = None) -> dict:
    """Schnellaktion "Erledigt": schliesst genau diesen Vorgang (andere bleiben offen)."""
    existing = _existing_by_ref(user, client_ref)
    if existing is not None or contact.status == DONE:
        customer = contact.customer
        return {"kind": "closed", "duplicate": True, "message": "Der Vorgang ist bereits erledigt.", "card": customer_card(user, customer)}
    now = utcnow_naive()
    contact.status = DONE
    contact.closed_by_user_id = user.id
    contact.closed_at = now
    contact.updated_by_user_id = user.id
    contact.updated_at = now
    note = CustomerNote(
        tenant_id=contact.tenant_id, customer_id=contact.customer_id, contact_id=contact.id,
        text="Erledigt" + (f" ({contact.topic})" if contact.topic else ""), event=DONE,
        created_by_user_id=user.id, created_at=now, client_ref=client_ref or None,
    )
    _note, created = _commit_note(note, user)
    customer = contact.customer
    return {
        "kind": "closed",
        "duplicate": not created,
        "message": f"Erledigt: {contact.topic or 'Vorgang'} bei {customer.name}.",
        "card": customer_card(user, customer),
    }


def _int(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        raise NoteError("Ungültige Angabe.") from None


# --- Fragen ---------------------------------------------------------------------------------


def _mentioned_members(text: str, customer: Customer) -> list[User]:
    """Mitarbeiter des Bueros, deren Vorname (oder voller Anzeigename) im Text steht."""
    from app.services.user_admin import office_members_query

    words = set(name_parts(text)) - set((customer.name_key or "").split())
    found = []
    for member in office_members_query().all():
        parts = name_parts(user_name(member)) if "@" not in user_name(member) else []
        if parts and parts[0] in words:
            found.append(member)
    return found


def answer_question(user, customer: Customer, text: str) -> dict:
    """Antwort ausschliesslich aus gespeicherten Notizen bzw. offenen Vorgaengen - mit Quelle."""
    if _OPEN_QUESTION.search(text) and not _REASON_QUESTION.search(text):
        contacts = open_contacts_of(customer)
        if not contacts:
            return {"text": f"Bei {customer.name} ist kein offener Kontaktvorgang dokumentiert.", "sources": []}
        return {
            "text": f"Offen bei {customer.name}:",
            "sources": [
                {"text": f"{c.topic or 'Anlass nicht angegeben'} – {OUTCOME_LABELS.get(c.outcome, 'offen')}"
                 + (f" ({c.attempts} Versuche)" if c.attempts > 1 else ""),
                 "author": user_name(c.updated_by or c.created_by), "when": when(c.updated_at), "source": "Offener Vorgang"}
                for c in contacts
            ],
        }
    members = _mentioned_members(text, customer)
    query = CustomerNote.query.filter(CustomerNote.customer_id == customer.id)
    if members:
        query = query.filter(CustomerNote.created_by_user_id.in_([m.id for m in members]))
    notes = query.order_by(CustomerNote.created_at.desc(), CustomerNote.id.desc()).limit(3).all()
    who = f" von {', '.join(user_name(m).split()[0] for m in members)}" if members else ""
    if not notes:
        return {"text": f"Zu {customer.name} ist kein Anlass{who} dokumentiert.", "sources": []}
    return {
        "text": f"Dokumentiert{who} zu {customer.name}:",
        "sources": [{"text": n.text, "author": user_name(n.created_by), "when": when(n.created_at), "source": "Notiz"} for n in notes],
    }


# --- Eingabe --------------------------------------------------------------------------------


def _choice(message: str, customers: list[Customer], create_option: bool = False, **extra) -> dict:
    return {
        "kind": "choose",
        "message": message,
        "candidates": [serialize_candidate(c) for c in customers[:MAX_CANDIDATES]],
        "more": len(customers) > MAX_CANDIDATES,
        "create_option": create_option,
        **extra,
    }


def _strip_identifiers(text: str, parsed: Parsed, customer: Customer | None) -> str:
    spans = list(parsed.vn_spans)
    rest = _remove_spans(text, spans)
    for phone_like in _PHONE_PATTERN.finditer(rest):
        if normalize_phone(phone_like.group(0)) in parsed.phones:
            rest = rest.replace(phone_like.group(0), " ", 1)
    for number in parsed.numbers:
        rest = re.sub(
            r"(?:kunden|versicherungsnehmer|vn)\s*-?\s*(?:nummer|nr\.?)\s*(?:ist|lautet|:)?\s*" + r"[\W_]*".join(map(re.escape, number)),
            " ",
            rest,
            flags=re.I,
        )
    if customer is not None:
        span = find_name_span(rest, customer)
        if span:
            rest = rest[: span[0]] + " " + rest[span[1]:]
    return rest


def handle_input(user, raw_text: str, *, selected_id=None, client_ref: str | None = None, mode: str | None = None,
                 customer_id=None, contact_choice=None, new_name=None, new_vn=None, create_new: bool = False) -> dict:
    """Verarbeitet eine Eingabe des ZentriqAI-Feldes. Ergebnis "kind":
    chat | saved | card | choose | choose_contact | conflict | not_found | clarify | need_name | need_vn.

    new_name / new_vn: auf Rueckfrage nachgereichte Angabe fuer eine Neuanlage. create_new: der
    Benutzer hat ausdruecklich "Neuen Kunden anlegen" gewaehlt (trotz gleichnamiger Kunden)."""
    text = " ".join((raw_text or "").split())
    if isinstance(new_vn, str) and new_vn.strip():
        vn = _valid_vn(new_vn)
        if vn is None:
            raise NoteError("Bitte eine gültige Versicherungsnehmernummer angeben (z. B. 808/123456-E).")
        text = f"{vn} {text}"
    forced_name = _valid_name(new_name) if isinstance(new_name, str) and new_name.strip() else None
    if isinstance(new_name, str) and new_name.strip() and forced_name is None:
        raise NoteError("Bitte Vor- und Nachnamen angeben.")
    if not text:
        raise NoteError("Bitte einen Text eingeben.")
    if len(text) > MAX_INPUT_CHARS:
        # Lange Texte sind keine Kurznotizen - unveraendert an den Chat.
        return {"kind": "chat"}

    # Ausdruecklich gewaehlter Kunde (Auswahlliste / Rueckfrage): kein erneutes Erkennen.
    if customer_id not in (None, ""):
        customer = Customer.query.filter(Customer.id == _int(customer_id)).first()
        if customer is None:
            raise NoteError("Der Kunde wurde nicht gefunden.", 404)
        return _act(user, customer, text, parsed=parse(text), client_ref=client_ref, mode=mode, contact_choice=contact_choice)

    parsed = parse(text)
    basis, candidates = find_by_identifiers(parsed)
    if basis is None:
        candidates = _by_name(text)
        basis = "name" if candidates else None

    selected = None
    if selected_id not in (None, ""):
        selected = Customer.query.filter(Customer.id == _int(selected_id)).first()

    question = is_question(text)
    chat_request = bool(_CHAT_REQUEST.search(text)) and not question

    if basis == "vn" and not candidates and not question and not chat_request:
        return _new_customer_from_vn(user, text, parsed, forced_name=forced_name, client_ref=client_ref,
                                     explicit=mode == "create", create_new=create_new)

    if basis is None:
        if parsed.has_identifier:
            label = "Versicherungsnehmernummer" if parsed.vn_keys else ("Kundennummer" if parsed.numbers else "Telefonnummer")
            return {"kind": "not_found", "message": f"Kein Kunde mit dieser {label} in Ihrem Büro gefunden. Es wurde nichts gespeichert."}
        if selected is not None:
            if question and not (_OPEN_QUESTION.search(text) or _REASON_QUESTION.search(text)):
                return {"kind": "chat"}
            if chat_request:
                return {"kind": "clarify", "message": f"Als Notiz für {selected.name} speichern oder an ZentriqAI senden?",
                        "customer": serialize_candidate(selected), "options": ["note", "chat"]}
            return _act(user, selected, text, parsed=parsed, client_ref=client_ref, mode=mode, contact_choice=contact_choice)
        if chat_request:
            return {"kind": "chat"}
        outcome, spans = detect_outcome(text)
        explicit_create = bool(_CREATE_REQUEST.search(text))
        if (outcome or explicit_create) and not question:
            # Notiz bzw. Anlageauftrag ohne erkennbaren Kunden: Teilnamen zur Auswahl anbieten;
            # ein genannter neuer Name braucht fuer die Neuanlage noch die VN-Nummer.
            name = extract_new_name(text, parsed)
            words = [w for w in name_parts(_remove_spans(text, spans)) if w not in _TOPIC_FILLER and len(w) >= 3]
            found = {c.id: c for word in words for c in search_by_name_fragment(word)}
            if found:
                return _choice("Welcher Kunde ist gemeint?", list(found.values()), pending_note=text,
                               create_option=bool(name))
            if name:
                return {"kind": "need_vn", "message": f"{name[0]} ist noch nicht angelegt. Versicherungsnehmernummer für die neue Kundenakte?",
                        "name": name[0], "pending_note": text}
            return {"kind": "not_found", "message": "Kein passender Kunde gefunden. Bitte Versicherungsnehmernummer oder vollständigen Namen angeben. Es wurde nichts gespeichert."}
        if not question and len(text.split()) <= 3 and not re.search(r"\d", text):
            # Reine Namenssuche ("Heller").
            fragment = search_by_name_fragment(text)
            if len(fragment) == 1:
                return _card_result(user, fragment[0], fragment[0].name)
            if fragment:
                return _choice("Mehrere Kunden passen – bitte auswählen:", fragment)
        return {"kind": "chat"}

    if not candidates:
        if chat_request:
            return {"kind": "chat"}
        return {"kind": "not_found", "message": "Kein passender Kunde gefunden. Es wurde nichts gespeichert."}

    if len(candidates) > 1:
        pending = None if question else (text if _strip_identifiers(text, parsed, None).strip() else None)
        reason = {"vn": "Mehrere Kunden gehören zu dieser Nummer", "phone": "Mehrere Kunden haben diese Telefonnummer",
                  "customer_number": "Mehrere Kunden haben diese Kundennummer", "name": "Mehrere Kunden heißen so"}[basis]
        return _choice(f"{reason} – bitte auswählen:", candidates, pending_note=pending)

    customer = candidates[0]
    if basis != "name":
        # Widerspruch: neben der Nummer wird ein anderer vorhandener Kunde mit vollem Namen genannt.
        others = [c for c in _by_name(_remove_spans(text, parsed.vn_spans)) if c.id != customer.id]
        if others and find_name_span(text, customer) is None:
            return {
                "kind": "conflict",
                "message": f"Die Nummer gehört zu {customer.name}, genannt ist aber {others[0].name}. Bitte den richtigen Kunden wählen.",
                "candidates": [serialize_candidate(c) for c in [customer, *others][:MAX_CANDIDATES]],
                "pending_note": None if question else text,
            }
    if chat_request:
        # "Schreib eine Mail an Justin Heller ..." ist ein Auftrag an die KI, keine Notiz. Mit
        # Kundenkennung ist beides denkbar -> kurz nachfragen statt zu speichern.
        if not parsed.has_identifier:
            return {"kind": "chat"}
        return {"kind": "clarify", "message": f"Als Notiz für {customer.name} speichern oder an ZentriqAI senden?",
                "customer": serialize_candidate(customer), "options": ["note", "chat"]}
    return _act(user, customer, text, parsed=parsed, client_ref=client_ref, mode=mode, contact_choice=contact_choice)


def _card_result(user, customer: Customer, message: str, answer: dict | None = None) -> dict:
    result = {"kind": "card", "message": message, "card": customer_card(user, customer)}
    if answer is not None:
        result["answer"] = answer
    return result


def _act(user, customer: Customer, text: str, *, parsed: Parsed, client_ref, mode, contact_choice) -> dict:
    if mode == "search" or (mode != "note" and is_question(text)):
        answer = answer_question(user, customer, text) if is_question(text) else None
        return _card_result(user, customer, customer.name, answer)
    remainder = _strip_identifiers(text, parsed, customer)
    note_text = normalize_note(remainder)
    if not re.search(r"[A-Za-zÄÖÜäöüß0-9]", note_text):
        if mode == "note":
            raise NoteError("Die Notiz enthält keinen Text.")
        return _card_result(user, customer, customer.name)
    return save_note(user, customer, note_text, client_ref=client_ref, contact_choice=contact_choice)


# --- Neuanlage aus einer Notiz ------------------------------------------------------------------
# Nur mit gueltiger VN-Nummer UND erkennbarem Namen, nur wenn im Buero kein Kunde mit dieser
# Nummer existiert und nur bei einer Notiz oder einem ausdruecklichen Anlageauftrag (nie bei einer
# reinen Suche). Gleichnamige Kunden -> Rueckfrage statt Neuanlage. Angelegt wird unter der
# Kundensperre des Bueros (app/services/customer_lock.py, MariaDB GET_LOCK) mit erneuter Pruefung
# nach dem Sperren; Kunde und Notiz werden in derselben Transaktion gespeichert.

MAX_NAME_LENGTH = 120
_CREATE_REQUEST = re.compile(r"\bneue[rn]?\s+kund(?:e|in)\b|\banlegen\b|\blege\b[^.?!]{0,80}\ban\b", re.I)
_CREATE_WORDS = re.compile(r"\b(?:neue[rn]?\s+kund(?:e|in)|kund(?:e|in)\s+anlegen|anlegen|lege\s+(?:bitte\s+)?(?:einen?\s+)?(?:neuen?\s+)?(?:kund(?:e|in|en)\s+)?an)\b[:,]?", re.I)
_NAME_TOKEN = re.compile(r"^[A-Za-zÄÖÜäöüßÉéÈèÁáÀàÓóÒòÚúÇç][A-Za-zÄÖÜäöüßÉéÈèÁáÀàÓóÒòÚúÇç'’-]*$")
_NAME_PARTICLES = frozenset({"von", "van", "de", "der", "zu", "vom", "den", "di", "da", "le", "la", "ten", "ter"})
# Woerter, die nie Namensbestandteil sind (Notizvokabular, Anrede, Anlageauftrag).
_NOT_NAME = _TOPIC_FILLER | frozenset(
    {
        "huk", "huk24", "kfz", "wechsel", "termin", "angebot", "nicht", "erreicht", "erreichbar", "bitte", "ruft", "rufen",
        "rueckruf", "rückruf", "zurueck", "zurück", "zurueckrufen", "zurückrufen", "meldet", "sich", "neuer", "neue",
        "neuen", "anlegen", "lege", "an", "notiz", "vertrag", "kuendigung", "kündigung", "schaden", "hausrat", "haftpflicht",
        "rechtsschutz", "unterlagen", "tarif", "beitrag", "mailbox", "per", "post", "mail", "email", "telefon", "tel", "name",
        "vn", "nummer", "versicherung", "versicherungsnehmer", "mit", "fuer", "für", "bei", "von", "im", "am", "um", "auf",
        "ich", "wir", "er", "sie", "es", "angerufen", "anruf", "info", "infos", "frage", "fragen", "ok", "okay", "ja", "nein",
    }
)


def _valid_vn(raw: str) -> str | None:
    match = _VN_PATTERN.fullmatch(" ".join(raw.split()))
    if match is None:
        return None
    number, digits, letter, suffix = match.groups()
    return f"{number}/{digits}-{letter.upper()}" + (f"-{suffix}" if suffix else "")


def _display_vn(parsed: Parsed, text: str) -> str:
    """VN-Nummer ohne Vertragsendung ("808/123456-E-12" -> "808/123456-E")."""
    start, end = parsed.vn_spans[0]
    vn = _valid_vn(text[start:end]) or text[start:end].upper()
    return _VN_DISPLAY.match(vn).group(1) if _VN_DISPLAY.match(vn) else vn


def _title(word: str) -> str:
    if word.lower() in _NAME_PARTICLES:
        return word.lower()
    if any(ch.isupper() for ch in word[1:]):
        return word  # bewusst so geschrieben ("McAllister")
    return "-".join(part[:1].upper() + part[1:].lower() for part in word.split("-"))


def _valid_name(raw: str) -> str | None:
    words = [w.strip(",.;:") for w in raw.split()]
    if not 2 <= len(words) <= 5 or not all(_NAME_TOKEN.match(w) for w in words):
        return None
    if len([w for w in words if w.lower() not in _NAME_PARTICLES]) < 2:
        return None
    name = " ".join(_title(w) for w in words)
    return name[:MAX_NAME_LENGTH] if len(name_parts(name)) >= 2 else None


_PARTICIPLE = re.compile(r"^ge\w+(?:t|en)$|\w+ier(?:t|en)$|\w+(?:ung|heit|keit)$", re.I)


def _name_from(tokens: list[re.Match], cased: bool = False) -> tuple[str, int, int] | None:
    """Vorname [Partikel] Nachname am Anfang von `tokens` (ohne Notizvokabular). cased: die
    Eingabe nutzt Gross-/Kleinschreibung - dann muessen Namensteile gross geschrieben sein."""
    words: list[re.Match] = []
    for token in tokens:
        word = token.group(0).strip(",.;:")
        if not _NAME_TOKEN.match(word):
            break
        lower = word.lower()
        if lower in _NAME_PARTICLES and words:
            words.append(token)
            continue
        if lower in _NOT_NAME or _PARTICIPLE.match(word) or (cased and not word[:1].isupper()):
            break
        words.append(token)
        if len([w for w in words if w.group(0).strip(",.;:").lower() not in _NAME_PARTICLES]) == 2:
            break
    while words and words[-1].group(0).strip(",.;:").lower() in _NAME_PARTICLES:
        words.pop()
    name = _valid_name(" ".join(w.group(0) for w in words)) if words else None
    if name is None:
        return None
    return name, words[0].start(), words[-1].start() + len(words[-1].group(0).rstrip(",.;:"))


def extract_new_name(text: str, parsed: Parsed) -> tuple[str, int, int] | None:
    """Name fuer eine Neuanlage: direkt hinter der VN-Nummer, sonst direkt davor bzw. am
    Textanfang. Liefert (Name, Start, Ende) oder None - es wird nichts geraten."""
    masked = text
    for start, end in parsed.vn_spans:  # gleich lang ausblenden: Positionen bleiben gueltig
        masked = masked[:start] + " " * (end - start) + masked[end:]
    masked = _CREATE_WORDS.sub(lambda m: " " * len(m.group(0)), masked)
    tokens = list(re.finditer(r"\S+", masked))
    cased = any(ch.isupper() for ch in masked)
    skip = {"herr", "herrn", "frau", "kunde", "kundin", "name", "name:"}
    if parsed.vn_spans:
        vn_start, vn_end = parsed.vn_spans[0]
        after = [t for t in tokens if t.start() >= vn_end]
        while after and after[0].group(0).strip(",.;:").lower() in skip:
            after.pop(0)
        found = _name_from(after, cased)
        if found:
            return found
        before = [t for t in tokens if t.end() <= vn_start]
        for size in (3, 2):
            window = before[-size:]
            if len(window) == size:
                found = _name_from(window, cased)
                if found and found[2] >= window[-1].end() - 1:
                    return found
        return None
    while tokens and tokens[0].group(0).strip(",.;:").lower() in skip:
        tokens.pop(0)
    return _name_from(tokens, cased)


def _note_after_creation(text: str, parsed: Parsed, name_span: tuple[int, int] | None, forced_name: str | None) -> str:
    spans = list(parsed.vn_spans)
    if name_span:
        spans.append(name_span)
    rest = _remove_spans(text, spans)
    if forced_name:
        rest = re.sub(r"\s+".join(map(re.escape, forced_name.split())), " ", rest, count=1, flags=re.I)
    rest = _CREATE_WORDS.sub(" ", rest)
    rest = re.sub(r"(?i)(^|\s)(?:herrn?|frau)(?=\s|$)", " ", rest)
    return normalize_note(rest)


# Woerter, die eine Kurznotiz eindeutig als Notiz ausweisen ("HUK24 Wechsel", "Termin").
_NOTE_NOUNS = re.compile(
    r"\b(?:wechsel|termin|angebot|vertrag|k(?:ü|ue)ndigung|schaden|unterlagen|beitrag|tarif|hausrat|haftpflicht|"
    r"rechtsschutz|versicherung|r(?:ü|ue)ckruf|anruf|angerufen|police|antrag|r(?:ü|ue)ckfrage|beratung)\w*",
    re.I,
)


def note_is_clear(note_text: str) -> bool:
    """Eindeutig eine Notiz: Kontaktstand, Fachbegriff oder mehrere Woerter. "HUK24" allein
    ist es nicht (koennte z. B. nur ein Stichwort zur Suche sein)."""
    if detect_outcome(note_text)[0] or _NOTE_NOUNS.search(note_text):
        return True
    return len(re.findall(r"[A-Za-zÄÖÜäöüß0-9]+", note_text)) >= 2


def name_boundary_unclear(text: str, parsed: Parsed, name_end: int) -> bool:
    """Folgt auf den erkannten Namen ein weiteres Wort, das ebenfalls ein Namensteil sein
    koennte ("Max Peter Mustermann ...", "Max Mustermann Police ..."), ist die Grenze zwischen
    Name und Notiz nicht sicher - dann wird nachgefragt statt geraten."""
    masked = text
    for start, end in parsed.vn_spans:
        masked = masked[:start] + " " * (end - start) + masked[end:]
    rest = masked[name_end:]
    if re.match(r"\s*[,;:–-]", rest):
        return False  # "Max Mustermann, HUK24 ..." - Grenze ausdruecklich gesetzt
    following = re.match(r"\s*(\S+)", rest)
    if following is None:
        return False
    word = following.group(1).strip(",.;:")
    cased = any(ch.isupper() for ch in masked)
    return bool(
        _NAME_TOKEN.match(word)
        and word.lower() not in _NOT_NAME
        and word.lower() not in _NAME_PARTICLES
        and not _PARTICIPLE.match(word)
        and not _NOTE_NOUNS.match(word)
        and (not cased or word[:1].isupper())
    )


def _new_customer_from_vn(user, text: str, parsed: Parsed, *, forced_name: str | None, client_ref, explicit: bool,
                          create_new: bool) -> dict:
    vn = _display_vn(parsed, text)
    found = None if forced_name else extract_new_name(text, parsed)
    if found and not explicit and name_boundary_unclear(text, parsed, found[2]):
        return {
            "kind": "need_name",
            "message": f"Kein Kunde mit {vn} gefunden. Wo endet der Name? Bitte den vollständigen Namen angeben.",
            "vn": vn,
            "pending_note": text,
            "suggestion": found[0],
        }
    name = forced_name or (found[0] if found else None)
    note_text = _note_after_creation(text, parsed, (found[1], found[2]) if found else None, forced_name)
    has_note = bool(re.search(r"[A-Za-zÄÖÜäöüß0-9]", note_text))
    explicit = explicit or bool(_CREATE_REQUEST.search(text))

    if not has_note and not explicit:
        # Reine Suche nach einer unbekannten Nummer: nichts anlegen, Anlegen nur anbieten.
        result = {"kind": "not_found", "message": f"Kein Kunde mit der Nummer {vn} in Ihrem Büro gefunden. Es wurde nichts angelegt."}
        if name:
            result["create"] = {"name": name, "vn": vn, "text": text}
        return result
    if name is None:
        return {"kind": "need_name", "message": f"Kein Kunde mit {vn} gefunden. Wie heißt der Kunde?", "vn": vn, "pending_note": text}
    if not create_new:
        same_name = Customer.query.filter(Customer.name_key == name_key(name)).order_by(Customer.name, Customer.id).limit(MAX_CANDIDATES + 1).all()
        if same_name:
            return _choice(
                f"Kein Kunde mit {vn}, aber es gibt bereits „{name}“. Ist es dieser Kunde oder ein neuer?",
                same_name,
                pending_note=text,
                create_option=True,
                create={"name": name, "vn": vn, "text": text},
            )
    if has_note and not explicit and not note_is_clear(note_text):
        # Nummer + Name + ein einzelnes Stichwort: Absicht (Notiz/Neuanlage oder Suche) unklar.
        return {
            "kind": "confirm_create",
            "message": f"{name} ({vn}) ist noch nicht angelegt. Mit Notiz „{note_text}“ anlegen?",
            "create": {"name": name, "vn": vn, "text": text},
        }
    return create_customer_with_note(user, name=name, vn=vn, note_text=note_text if has_note else None, client_ref=client_ref)


def create_customer_with_note(user, *, name: str, vn: str, note_text: str | None, client_ref: str | None) -> dict:
    """Legt den Kunden an und speichert die Notiz - eine Transaktion, unter der Kundensperre."""
    existing_note = _existing_by_ref(user, client_ref)
    if existing_note is not None:
        return _saved_result(user, existing_note, duplicate=True)
    vn_key = "".join(ch for ch in vn.upper() if ch.isalnum())
    lock = customer_creation_lock(user.tenant_id, timeout_seconds=20)
    try:
        lock.acquire()
    except CustomerLockTimeout:
        raise NoteError("Gerade werden Kundendaten gespeichert. Bitte in einigen Sekunden erneut versuchen.", 409) from None
    try:
        fresh_transaction()
        # Erneut pruefen: eine parallele Anfrage (Doppelklick, zweiter Mitarbeiter) kann den
        # Kunden inzwischen angelegt haben - dann nur die Notiz bei diesem Kunden speichern.
        owners = _load_customers(customers_by_vn(vn_key))
        if owners:
            customer = owners[0] if len(owners) == 1 else None
            if customer is None:
                return _choice("Mehrere Kunden gehören zu dieser Nummer – bitte auswählen:", owners, pending_note=note_text)
            if note_text:
                return save_note(user, customer, note_text, client_ref=client_ref)
            return _card_result(user, customer, f"{customer.name} ist bereits angelegt.")
        customer = Customer(
            tenant_id=user.tenant_id,
            name=name,
            customer_number=vn[:50],
            assigned_user_id=user.id,
            source=customer_sources.NOTIZ,
            field_sources={"name": customer_sources.NOTIZ, "customer_number": customer_sources.NOTIZ},
        )
        db.session.add(customer)
        try:
            db.session.flush()
            if note_text:
                result = save_note(user, customer, note_text, client_ref=client_ref)  # committet Kunde + Notiz
            else:
                db.session.commit()
                result = _card_result(user, customer, "")
        except Exception:
            db.session.rollback()
            raise
    finally:
        lock.release()
    from app.models.audit_log import AuditEventType
    from app.services.audit import log_audit_event

    log_audit_event(AuditEventType.CUSTOMER_CREATED, user=user, details={"customer_id": customer.id, "source": customer_sources.NOTIZ})
    result["created"] = True
    result["kind"] = "saved" if note_text else "card"
    result["message"] = f"{customer.name} wurde angelegt." + (f" Notiz gespeichert: {note_text}." if note_text else "")
    result["card"] = customer_card(user, customer)
    return result


def without_ai(user, raw_text: str) -> dict:
    """Die Eingabe braeuchte das KI-Modell, es ist aber keins verfuegbar. Sieht sie nach einer
    Kundennotiz aus (Name + Notiz, nur die VN-Nummer fehlt), wird nur diese abgefragt; sonst ein
    kurzer Hinweis - die Kundenfunktionen bleiben nutzbar."""
    text = " ".join((raw_text or "").split())
    parsed = parse(text)
    found = extract_new_name(text, parsed) if not parsed.has_identifier and not is_question(text) else None
    if found:
        rest = normalize_note(_remove_spans(text, [(found[1], found[2])]))
        if rest and note_is_clear(rest):
            return {
                "kind": "need_vn",
                "message": f"{found[0]} ist nicht angelegt. Versicherungsnehmernummer für die neue Kundenakte?",
                "name": found[0],
                "pending_note": text,
            }
    return {
        "kind": "no_ai",
        "message": "Für diese Anfrage wird ein KI-Modell benötigt, derzeit ist keins verfügbar. Kundennotizen, Suche "
        "und Rückrufe funktionieren weiterhin, z. B. „808/123456-E Max Mustermann HUK24 Wechsel“.",
    }
