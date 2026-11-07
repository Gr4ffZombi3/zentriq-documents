"""Ausdrueckliche Kundenanlage im ZentriqAI-Eingabefeld - rein lokal, ohne KI.

Beispiel: "lege kunden justin heller an 01795223152 wilhelmstrasse 7 53809"
-> Vorname Justin, Nachname Heller, Telefon 0179 5223152, Wilhelmstrasse 7, PLZ 53809.

Ablauf (handle):
1. Anlagebefehl erkennen ("Lege Kunden ... an", "Kunde(n) anlegen ...", "Neuer Kunde ...") -
   wird vor der normalen Kundensuche geprueft. Reine Suchen ("Justin Heller", "0179...") sind
   keine Befehle.
2. Angaben nach festen Regeln herausloesen: E-Mail, VN-Nummer, Telefon (einheitliche
   Normalisierung aus app/utils/customer_keys.py), PLZ (+ Ort, falls genannt), Strasse +
   Hausnummer; uebrig bleibende Woerter bilden den Namen.
3. Name: zwei Woerter = Vorname Nachname; "Nachname, Vorname"; Namenszusaetze (von, van, de ...)
   beginnen den Nachnamen. Sonst (ein Wort, drei Woerter ohne Zusatz ...) wird kurz nachgefragt -
   die erkannten Angaben bleiben erhalten (die Rueckfrage schickt denselben Text + Namensteile).
4. Bestand im eigenen Buero pruefen (Tenant-Filter): VN-Nummer, sonst gleicher Name ohne
   widersprechende Angaben -> vorhandene Karte zeigen, nichts ueberschreiben. Eine gleiche
   Telefonnummer allein ist KEIN Grund fuer eine Zuordnung (Familien teilen Nummern).
5. Sonst anlegen - unter der Anlagesperre des Bueros mit erneuter Pruefung, damit
   Doppelklick/Wiederholung keine Dublette erzeugen. Keine Notiz, kein Rueckruf. Gemeldet wird
   erst nach dem Commit: "Justin Heller wurde angelegt.\""""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from flask import current_app

from app.extensions import db
from app.models import Customer
from app.services import customer_sources
from app.services.customer_lock import CustomerLockTimeout, customer_creation_lock, fresh_transaction
from app.utils.customer_keys import (
    VN_PATTERN,
    format_phone,
    name_key,
    normalize_customer_number,
    normalize_phone,
)

# --- Befehl ------------------------------------------------------------------------------------

_KUNDE = r"(?:neu)?kund(?:e|en|in)"
_COMMANDS = (
    # "Lege (bitte) (einen) (neuen) Kunden ... an", "Leg Kundin ... an"
    re.compile(rf"^\s*(?:bitte\s+)?leg(?:e)?\s+(?:bitte\s+)?(?:(?:einen|eine|den|die)\s+)?(?:neue[nr]?\s+)?{_KUNDE}\b", re.I),
    # "Lege Justin Heller als (neuen) Kunden an"
    re.compile(rf"^\s*(?:bitte\s+)?leg(?:e)?\s+.+?\bals\s+(?:neue[nr]?\s+)?{_KUNDE}\s+an\b", re.I),
    # "Neuer Kunde ...", "Neue Kundin: ...", "Neukunde ..."
    re.compile(r"^\s*(?:bitte\s+)?(?:neue[rn]?\s+kund(?:e|in)|neukund(?:e|in))\b", re.I),
    # "Kunde(n) anlegen ...", "Kunde Justin Heller anlegen"
    re.compile(rf"^\s*(?:bitte\s+)?(?:(?:einen|eine|neue[nr]?)\s+)?{_KUNDE}\s+anlegen\b", re.I),
    re.compile(rf"^\s*(?:bitte\s+)?{_KUNDE}\b.+\banlegen\s*[.!]?\s*$", re.I),
)
# Befehlswoerter, die nicht zum Namen gehoeren. "an" nur als Verbzusatz (nicht "An der Kirche 5").
_COMMAND_WORDS = re.compile(
    rf"\b(?:bitte|leg(?:e)?|einen|eine|neue[nrs]?|{_KUNDE}|anlegen|als)\b|^\s*(?:den|die)\b", re.I
)
_PARTICLE_AN = re.compile(r"(?<!\S)an(?=[\s:,.;]|$)(?!\s+(?:der|dem|den|die)\s)", re.I)
_LABELS = re.compile(
    r"(?<!\S)(?:tel\.?|telefon(?:nummer)?|handy(?:nummer)?|mobil(?:nummer)?|rufnummer|nr\.?|nummer|plz|postleitzahl|"
    r"adresse|anschrift|wohnhaft|wohnt|stra(?:ss|ß)e:|ort|e-?mail|mail|vn(?:-?nr\.?)?|versicherungsnehmer(?:nummer)?|"
    r"name|vorname|nachname|herr|frau|und|mit|in|aus|,|:|;|-)(?=\s|$)",
    re.I,
)

# --- Angaben -----------------------------------------------------------------------------------

_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_PHONE = re.compile(r"(?<![\w/+])(?:\+\d{1,3}|0)[\d()]*(?:(?:\s*[/-]\s*|\s)?\(?\d+\)?)*")
_PLZ_CITY = re.compile(
    r"(?<![\w/])(\d{5})(?![\w/])"
    r"(?:\s+((?:bad|sankt|st\.)\s+[a-zäöüß][\w.\-äöüß]*|[a-zäöüß][\w.\-äöüß]*"
    r"(?:\s+(?:am|an\s+der|im|ob\s+der|in\s+der|bei|vor\s+der)\s+[a-zäöüß][\w.\-äöüß]*)?)"
    # Ort nur am Ende bzw. vor Komma/Semikolon - "53809 Justin Heller" ist kein Ort "Justin".
    r"(?=\s*(?:[,;]|$)))?",
    re.I,
)
_HOUSE = r"(\d{1,4}(?:\s?[a-z])?(?:\s*[-/]\s*\d{1,4}[a-z]?)?)(?![\d\w])"
_STREET_SUFFIX_STRONG = r"(?:stra(?:ss|ß)e|str\.?)"
_STREET_SUFFIX = r"(?:stra(?:ss|ß)e|str\.?|weg|gasse|allee|platz|ring|damm|ufer|chaussee|pfad|steig|markt|wall|graben|promenade|zeile|hof)"
_STREETS = (
    # "Bonner Straße 5", "Alte Poststr. 3"
    re.compile(rf"(?<!\S)([a-zäöüß][\w\-äöüß]*\s+{_STREET_SUFFIX})\.?(?:\s+|\s*,\s*){_HOUSE}", re.I),
    # "Wilhelmstrasse 7", "Lindenweg 12a"
    re.compile(rf"(?<!\S)([a-zäöüß][\w\-äöüß]*{_STREET_SUFFIX})\.?(?:\s+|\s*,\s*){_HOUSE}", re.I),
    # "Am Markt 3", "An der Kirche 5", "Im Ellig 4"
    re.compile(rf"(?<!\S)((?:am|an\s+der|an\s+den|an\s+dem|im|in\s+der|in\s+den|auf\s+der|auf\s+dem|zum|zur|unter\s+den|hinter\s+der|vor\s+dem)\s+[a-zäöüß][\w\-äöüß]*)\s+{_HOUSE}", re.I),
    # Strasse ohne Hausnummer: nur eindeutige Endungen ("Wilhelmstraße")
    re.compile(rf"(?<!\S)([a-zäöüß][\w\-äöüß]*{_STREET_SUFFIX_STRONG})\.?(?=\s|$|,)()", re.I),
)
_NAME_TOKEN = re.compile(r"^[A-Za-zÄÖÜäöüßÀ-ÿ][A-Za-zÄÖÜäöüßÀ-ÿ'’-]*$")
_PARTICLES = ("von", "van", "de", "der", "den", "zu", "vom", "zum", "di", "da", "le", "la", "ten", "ter", "del", "dos")
_LOWER_WORDS = {"am", "an", "der", "dem", "den", "die", "im", "in", "auf", "zum", "zur", "unter", "hinter", "vor", "ob", "bei"}


def is_command(text: str) -> bool:
    return any(rx.search(text or "") for rx in _COMMANDS)


@dataclass
class Draft:
    first_name: str | None = None
    last_name: str | None = None
    phone: str | None = None  # normalisiert "+49..."
    street: str | None = None
    house_number: str | None = None
    postal_code: str | None = None
    city: str | None = None
    email: str | None = None
    vn: str | None = None
    name_tokens: list[str] = field(default_factory=list)
    name_options: list[tuple[str, str]] = field(default_factory=list)
    not_taken: list[str] = field(default_factory=list)

    @property
    def name(self) -> str | None:
        if self.first_name and self.last_name:
            return f"{self.first_name} {self.last_name}"
        return None

    @property
    def address(self) -> str | None:
        return " ".join(filter(None, [self.street, self.house_number])) or None

    def facts(self) -> list[str]:
        out = []
        if self.phone:
            out.append(f"Telefon {format_phone(self.phone)}")
        if self.address:
            out.append(self.address)
        place = " ".join(filter(None, [self.postal_code, self.city]))
        if place:
            out.append(place)
        if self.email:
            out.append(self.email)
        if self.vn:
            out.append(f"VN {self.vn}")
        return out


def _cap(word: str) -> str:
    """Schreibweise: komplett klein/gross Eingegebenes wird je Bindestrich-Teil gross begonnen;
    gemischte Schreibweise (McDonald) bleibt."""
    if word.lower() in _PARTICLES:
        return word.lower()
    if word.islower() or word.isupper():
        return "-".join("'".join(p[:1].upper() + p[1:].lower() for p in part.split("'")) for part in word.split("-"))
    return word


def _cap_place(text: str) -> str:
    words = text.split()
    out = []
    for i, w in enumerate(words):
        if i and w.lower() in _LOWER_WORDS:
            out.append(w.lower())
        elif w.islower() or w.isupper():
            out.append(w[:1].upper() + w[1:].lower())
        else:
            out.append(w)
    return " ".join(out)


def _mask(text: str, start: int, end: int) -> str:
    return text[:start] + " " * (end - start) + text[end:]


def _phone_candidate(raw: str) -> str | None:
    """Telefonnummer aus einer Ziffernfolge; eine angehaengte PLZ ("0179 5223152 53809") wird
    abgetrennt (deutsche Rufnummern haben national hoechstens 12 Ziffern)."""
    groups = [g for g in re.split(r"[ \t/()-]+", raw.strip()) if g]
    # National hoechstens 12 Ziffern, international (+49 / 0049) hoechstens 15.
    limit = 15 if raw.strip().startswith(("+", "00")) else 12
    while groups:
        digits = re.sub(r"\D", "", "".join(groups))
        if len(digits) <= limit and normalize_phone("".join(groups)):
            return "".join(groups)
        if len(groups) == 1:
            return None
        groups.pop()
    return None


def parse(text: str) -> Draft:
    draft = Draft()
    work = " " + " ".join((text or "").split()) + " "
    # Befehlswoerter entfernen.
    work = _COMMAND_WORDS.sub(lambda m: " " * len(m.group(0)), work)
    m = _PARTICLE_AN.search(work)
    if m:
        work = _mask(work, m.start(), m.end())

    m = _EMAIL.search(work)
    if m:
        draft.email = m.group(0).lower()
        work = _mask(work, m.start(), m.end())
    m = VN_PATTERN.search(work)
    if m:
        draft.vn = f"{m.group(1)}/{m.group(2)}-{m.group(3).upper()}"
        work = _mask(work, m.start(), m.end())

    for m in list(_PHONE.finditer(work)):
        candidate = _phone_candidate(m.group(0))
        if not candidate:
            continue
        # Ende der tatsaechlich verwendeten Ziffern im Original bestimmen.
        used, pos = 0, m.start()
        while pos < m.end() and used < len(candidate):
            if work[pos].isdigit() or work[pos] == "+":
                used += 1
            pos += 1
        end = pos
        normalized = normalize_phone(candidate)
        if draft.phone is None:
            draft.phone = normalized
        elif normalized != draft.phone:
            draft.not_taken.append(format_phone(normalized))
        work = _mask(work, m.start(), end)

    for rx in _STREETS:
        m = rx.search(work)
        if m:
            draft.street = _cap_place(m.group(1).strip().rstrip(","))
            draft.house_number = re.sub(r"\s+", "", m.group(2)) if m.group(2) else None
            work = _mask(work, m.start(), m.end())
            break

    m = _PLZ_CITY.search(work)
    if m:
        draft.postal_code = m.group(1)
        city = m.group(2)
        if city and not _LABELS.fullmatch(city.split()[0]) and not re.search(r"\d", city):
            draft.city = _cap_place(city)
            work = _mask(work, m.start(), m.end())
        else:
            work = _mask(work, m.start(1), m.end(1))

    # Komma-Schreibweise "Heller, Justin" vor dem Entfernen der Satzzeichen erkennen.
    comma = re.search(r"([^\s,]+)\s*,\s*([^\s,]+)", work)
    tokens = []
    for raw in work.replace(",", " , ").split():
        word = raw.strip(".;:!?\"'()")
        if not word or _LABELS.fullmatch(word):
            continue
        if _NAME_TOKEN.match(word):
            tokens.append(word)
        else:
            draft.not_taken.append(word)
    draft.name_tokens = tokens
    words = [_cap(t) for t in tokens]
    if len(words) == 2 and comma and [comma.group(1).strip(".,"), comma.group(2).strip(".,")] == tokens:
        draft.last_name, draft.first_name = words[0], words[1]
    elif len(words) == 2:
        draft.first_name, draft.last_name = words
    elif len(words) >= 3:
        particle = next((i for i, w in enumerate(words) if i > 0 and w.lower() in _PARTICLES), None)
        if particle is not None and particle < len(words) - 1:
            draft.first_name, draft.last_name = " ".join(words[:particle]), " ".join(words[particle:])
        elif len(words) <= 4:
            draft.name_options = [(" ".join(words[:-1]), words[-1]), (words[0], " ".join(words[1:]))]
    return draft


# --- Ablauf ------------------------------------------------------------------------------------


class CreateError(ValueError):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _clean_name_part(value) -> str | None:
    if not isinstance(value, str):
        return None
    words = [w for w in value.split() if w]
    if not words or len(words) > 4 or not all(_NAME_TOKEN.match(w) for w in words):
        return None
    return " ".join(_cap(w) for w in words)


def _conflicts(customer: Customer, draft: Draft) -> list[str]:
    """Angaben, die einem vorhandenen gleichnamigen Kunden widersprechen."""
    out = []
    if draft.phone and customer.phone_key and customer.phone_key != draft.phone:
        out.append("Telefon")
    if draft.postal_code and customer.postal_code and customer.postal_code.strip() != draft.postal_code:
        out.append("PLZ")
    street = (customer.street or customer.address or "").lower().replace("ß", "ss")
    if draft.street and street and draft.street.lower().replace("ß", "ss") not in street:
        out.append("Straße")
    if draft.vn and customer.customer_number and normalize_customer_number(customer.customer_number) != normalize_customer_number(draft.vn):
        out.append("VN-Nummer")
    return out


def _missing_in(customer: Customer, draft: Draft) -> list[str]:
    """Neue Angaben, die beim vorhandenen Kunden fehlen (werden NICHT ungefragt eingetragen)."""
    out = []
    if draft.phone and not customer.phone:
        out.append("Telefon")
    if draft.street and not (customer.street or customer.address):
        out.append("Adresse")
    if draft.postal_code and not customer.postal_code:
        out.append("PLZ")
    if draft.email and not customer.email:
        out.append("E-Mail")
    return out


def _existing(draft: Draft) -> tuple[list[Customer], list[Customer]]:
    """(eindeutig passende, gleichnamige mit Widerspruch) im eigenen Buero."""
    from app.services.customer_notes import _load_customers, customers_by_vn

    if draft.vn:
        owners = _load_customers(customers_by_vn(normalize_customer_number(draft.vn)))
        if owners:
            return owners, []
    same = (
        Customer.query.filter(Customer.name_key == name_key(draft.name))
        .order_by(Customer.id)
        .limit(6)
        .all()
    )
    fitting = [c for c in same if not _conflicts(c, draft)]
    return fitting, [c for c in same if c not in fitting]


def _draft_payload(text: str, draft: Draft) -> dict:
    return {"text": text, "facts": draft.facts(), "first_name": draft.first_name, "last_name": draft.last_name}


def handle(user, text: str, *, first_name=None, last_name=None, create_new: bool = False) -> dict:
    from app.services.customer_notes import _choice

    draft = parse(text)
    if first_name is not None or last_name is not None:
        first, last = _clean_name_part(first_name), _clean_name_part(last_name)
        if not first or not last:
            raise CreateError("Bitte Vor- und Nachnamen angeben (nur Buchstaben, Bindestrich, Apostroph).")
        draft.first_name, draft.last_name, draft.name_options = first, last, []

    if draft.name is None:
        known = " ".join(_cap(t) for t in draft.name_tokens)
        if draft.name_options:
            message = f"Wie ist „{known}“ aufgeteilt?"
        elif draft.name_tokens:
            message = f"Bitte Vor- und Nachnamen für „{known}“ angeben."
        else:
            message = "Wie heißt der neue Kunde? Bitte Vor- und Nachnamen angeben."
        return {
            "kind": "create_name",
            "message": message,
            "options": [{"first_name": f, "last_name": last, "label": f"Vorname „{f}“, Nachname „{last}“"} for f, last in draft.name_options],
            "suggestion": {
                "first_name": _cap(draft.name_tokens[0]) if len(draft.name_tokens) > 1 else "",
                "last_name": _cap(draft.name_tokens[-1]) if draft.name_tokens else "",
            },
            "create": _draft_payload(text, draft),
        }

    if not create_new:
        fitting, conflicting = _existing(draft)
        if len(fitting) == 1:
            return _existing_result(user, fitting[0], draft)
        if fitting or conflicting:
            reason = "Mehrere Kunden passen" if len(fitting) > 1 else f"Es gibt bereits „{draft.name}“ mit anderen Angaben"
            return _choice(
                f"{reason} – vorhandenen Kunden öffnen oder neu anlegen?",
                (fitting or conflicting)[:5],
                create_option=True,
                create={"name": draft.name, "text": text, "first_name": draft.first_name, "last_name": draft.last_name},
            )
    return _create(user, draft, text, create_new=create_new)


def _existing_result(user, customer: Customer, draft: Draft) -> dict:
    from app.services.customer_notes import customer_card

    missing = _missing_in(customer, draft)
    notice = f"{customer.name} ist bereits angelegt."
    if missing:
        notice += f" Nicht übernommen ({', '.join(missing)}) – bei Bedarf über „Bearbeiten“ ergänzen."
    return {"kind": "card", "existing": True, "notice": notice, "message": customer.name, "card": customer_card(user, customer)}


def _create(user, draft: Draft, text: str, *, create_new: bool) -> dict:
    from app.services.customer_notes import customer_card

    lock = customer_creation_lock(user.tenant_id, timeout_seconds=20)
    try:
        lock.acquire()
    except CustomerLockTimeout:
        raise CreateError("Gerade werden Kundendaten gespeichert. Bitte in einigen Sekunden erneut versuchen.", 409) from None
    try:
        fresh_transaction()
        # Erneut pruefen (Doppelklick, zweiter Mitarbeiter): identische Anlage nur einmal.
        fitting, _conflicting = _existing(draft)
        twin = next((c for c in fitting if not _missing_in(c, draft) and (not create_new or _same_data(c, draft))), None)
        if twin is not None and (not create_new or _recent(twin)):
            return _existing_result(user, twin, draft)
        values = {
            "name": draft.name,
            "first_name": draft.first_name,
            "last_name": draft.last_name,
            "phone": format_phone(draft.phone) if draft.phone else None,
            "street": draft.street,
            "house_number": draft.house_number,
            "address": draft.address,
            "postal_code": draft.postal_code,
            "city": draft.city,
            "email": draft.email,
            "customer_number": draft.vn,
        }
        values = {k: v for k, v in values.items() if v}
        customer = Customer(
            tenant_id=user.tenant_id,
            assigned_user_id=user.id,
            source=customer_sources.EINGABE,
            field_sources={k: customer_sources.EINGABE for k in values},
            **values,
        )
        db.session.add(customer)
        try:
            db.session.commit()
        except Exception:
            db.session.rollback()
            raise
    finally:
        lock.release()

    from app.models.audit_log import AuditEventType
    from app.services.audit import log_audit_event

    try:
        log_audit_event(AuditEventType.CUSTOMER_CREATED, user=user, details={"customer_id": customer.id, "source": customer_sources.EINGABE})
    except Exception:  # noqa: BLE001 - der Kunde ist gespeichert
        current_app.logger.exception("customer_create.audit_failed customer=%s", customer.id)
        db.session.rollback()
    shared = [c for c in Customer.query.filter(Customer.phone_key == customer.phone_key, Customer.id != customer.id).limit(3)] if customer.phone_key else []
    notice = []
    if shared:
        notice.append(f"Hinweis: Diese Telefonnummer ist auch bei {', '.join(c.name for c in shared)} hinterlegt.")
    if draft.not_taken:
        notice.append(f"Nicht übernommen: {', '.join(draft.not_taken)}.")
    return {
        "kind": "card",
        "created": True,
        "message": f"{customer.name} wurde angelegt.",
        "notice": " ".join(notice) or None,
        "card": customer_card(user, customer),
    }


def _same_data(customer: Customer, draft: Draft) -> bool:
    return not _conflicts(customer, draft) and not _missing_in(customer, draft)


def _recent(customer: Customer) -> bool:
    """Bei ausdruecklichem "neu anlegen" gilt ein gleicher Datensatz nur als Wiederholung, wenn er
    gerade eben (Doppelklick) entstanden ist."""
    from datetime import datetime, timedelta, timezone

    created = customer.created_at
    if created is None:
        return False
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - created < timedelta(seconds=30)


# --- Bearbeiten ueber die Kundenkarte ----------------------------------------------------------

EDIT_FIELDS = ("first_name", "last_name", "name", "phone", "street", "house_number", "address", "postal_code", "city", "email", "customer_number")


def can_edit(user, customer: Customer) -> bool:
    """Buero-Admins alle Kunden des Bueros; Mitarbeiter die Kunden, die sie selbst angelegt haben."""
    return bool(user.is_office_admin or (user.is_employee and customer.assigned_user_id == user.id))


def version_of(customer: Customer) -> str:
    return customer.updated_at.isoformat() if customer.updated_at else ""


def edit_payload(customer: Customer) -> dict:
    structured_name = bool(customer.first_name or customer.last_name) or not customer.name
    structured_address = bool(customer.street or customer.house_number) or not customer.address
    return {
        "structured_name": structured_name,
        "structured_address": structured_address,
        "values": {key: getattr(customer, key) or "" for key in EDIT_FIELDS},
        "version": version_of(customer),
    }


def update_fields(user, customer: Customer, data: dict) -> Customer:
    from app.services.customer_notes import customers_by_vn
    from app.utils.customer_keys import vn_key

    if not can_edit(user, customer):
        raise CreateError("Diese Kundendaten dürfen Sie nicht bearbeiten.", 403)
    if (data.get("version") or "") != version_of(customer):
        raise CreateError("Der Kunde wurde inzwischen geändert. Bitte die Karte neu laden.", 409)

    def text(key, limit):
        value = data.get(key)
        if value is None:
            return getattr(customer, key)
        if not isinstance(value, str):
            raise CreateError("Ungültige Eingabe.")
        value = " ".join(value.split())
        if len(value) > limit:
            raise CreateError("Eine Angabe ist zu lang.")
        return value or None

    values = {}
    if "first_name" in data or "last_name" in data:
        first, last = _clean_name_part(data.get("first_name") or ""), _clean_name_part(data.get("last_name") or "")
        if not first or not last:
            raise CreateError("Bitte Vor- und Nachnamen angeben.")
        values.update(first_name=first, last_name=last, name=f"{first} {last}")
    elif "name" in data:
        name = text("name", 255)
        if not name:
            raise CreateError("Der Name darf nicht leer sein.")
        values["name"] = name
    phone = text("phone", 50)
    if phone and not normalize_phone(phone):
        raise CreateError("Die Telefonnummer wurde nicht erkannt (mit Vorwahl angeben).")
    values["phone"] = format_phone(normalize_phone(phone)) if phone else None
    postal = text("postal_code", 20)
    if postal and not re.fullmatch(r"\d{5}", postal):
        raise CreateError("Die Postleitzahl muss fünfstellig sein.")
    values["postal_code"] = postal
    email = text("email", 255)
    if email and not _EMAIL.fullmatch(email):
        raise CreateError("Die E-Mail-Adresse ist ungültig.")
    values["email"] = email.lower() if email else None
    values["city"] = text("city", 120)
    if "street" in data or "house_number" in data:
        street, number = text("street", 200), text("house_number", 20)
        values.update(street=street, house_number=number, address=" ".join(filter(None, [street, number])) or None)
    elif "address" in data:
        values["address"] = text("address", 255)
    vn = text("customer_number", 50)
    if vn:
        key = vn_key(vn)
        if key is None:
            raise CreateError("Bitte eine gültige Versicherungsnehmernummer angeben (z. B. 808/123456-E).")
        vn = f"{key[:3]}/{key[3:9]}-{key[9]}"
        if any(owner != customer.id for owner in customers_by_vn(key)):
            raise CreateError("Diese Versicherungsnehmernummer gehört bereits zu einem anderen Kunden.", 409)
    values["customer_number"] = vn

    sources = dict(customer.field_sources or {})
    changed = []
    for key, value in values.items():
        if getattr(customer, key) != value:
            setattr(customer, key, value)
            sources[key] = customer_sources.MANUELL
            changed.append(key)
    if changed:
        customer.field_sources = sources
        db.session.commit()
        current_app.logger.info("customer.edited customer_id=%s user_id=%s fields=%s", customer.id, user.id, ",".join(changed))
    return customer
