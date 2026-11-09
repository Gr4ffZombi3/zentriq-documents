"""Ausdrueckliche Kundenanlage im ZentriqAI-Eingabefeld - rein lokal, ohne KI.

Beispiel: "lege kunden justin heller an festnetz 02641 12345, privat 01795223152 wilhelmstrasse 7 53809"
-> Vorname Justin, Nachname Heller, Festnetz 02641 12345, Privattelefon 0179 5223152, ...

Telefon: zwei optionale Felder Festnetz und Privattelefon. Zugeordnet wird nur ueber ein
vorangestelltes Stichwort ("Festnetz", "privat"/"Privattelefon"). Ohne Stichwort wird der Typ nicht
geraten - der Assistent fragt nach (phone_kinds).

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
    # Verkuerzt: "Kunde Max Mustermann anlegen privat 0171 ..." (Angaben nach "anlegen")
    re.compile(rf"^\s*(?:bitte\s+)?(?:(?:einen|eine|neue[nr]?)\s+)?{_KUNDE}\s+[A-Za-zÄÖÜäöüß'’-]+(?:\s+[A-Za-zÄÖÜäöüß'’-]+){{0,3}}\s+anlegen\b", re.I),
    # "Max Mustermann als (neuen) Kunden aufnehmen/anlegen ..."
    re.compile(rf"\bals\s+(?:neue[nr]?\s+)?{_KUNDE}\s+(?:anlegen|aufnehmen)\b", re.I),
)
# Klingt nach Neuanlage, passt aber auf keine feste Regel -> hoechstens ein kurzer Modellaufruf.
_CREATE_HINT = re.compile(rf"\b{_KUNDE}\b.*\b(?:anleg|aufnehm|erfass)\w*|\b(?:anleg|aufnehm|erfass)\w*\b.*\b{_KUNDE}\b|\bneukund", re.I)
# Befehlswoerter, die nicht zum Namen gehoeren. "an" nur als Verbzusatz (nicht "An der Kirche 5").
_COMMAND_WORDS = re.compile(
    rf"\b(?:bitte|leg(?:e)?|einen|eine|neue[nrs]?|{_KUNDE}|anlegen|aufnehmen|als)\b|^\s*(?:den|die)\b", re.I
)
_PARTICLE_AN = re.compile(r"(?<!\S)an(?=[\s:,.;]|$)(?!\s+(?:der|dem|den|die)\s)", re.I)
_LABELS = re.compile(
    r"(?<!\S)(?:tel\.?|telefon(?:nummer)?|handy(?:nummer)?|mobil(?:nummer)?|rufnummer|nr\.?|nummer|plz|postleitzahl|"
    r"adresse|anschrift|wohnhaft|wohnt|stra(?:ss|ß)e:|ort|e-?mail|mail|vn(?:-?nr\.?)?|versicherungsnehmer(?:nummer)?|"
    r"name|vorname|nachname|herr|frau|und|mit|in|aus|festnetz(?:nummer)?|privat(?:telefon|nummer)?|private|,|:|;|-)(?=\s|$)",
    re.I,
)

# --- Angaben -----------------------------------------------------------------------------------

_PHONE_KIND = re.compile(r"(?:^|[\s,;:(])(festnetz(?:nummer|telefon)?|privat(?:telefon|nummer)?|private\s+nummer|priv\.)\s*[:=]?\s*$", re.I)
PHONE_KINDS = {"festnetz": "Festnetz", "privat": "Privattelefon"}
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


def looks_like_create(text: str) -> bool:
    return bool(_CREATE_HINT.search(text or "")) and not is_command(text) and len(text or "") <= 300


_EXTRACT_SYSTEM = (
    "Du extrahierst aus einer kurzen deutschen Eingabe eines Versicherungsbueros, ob ein neuer Kunde angelegt werden soll. "
    'Antworte NUR mit JSON: {"anlegen": true|false, "vorname": "", "nachname": "", "festnetz": "", "privat": "", '
    '"telefon_ohne_typ": "", "strasse": "", "hausnummer": "", "plz": "", "ort": "", "email": ""}. '
    "Nichts erfinden; unbekannte Felder leer lassen. Eine Telefonnummer nur dann als festnetz/privat, wenn das Wort ausdruecklich davor steht."
)


def handle_with_model(user, text: str) -> dict:
    """Unklare Anlage-Formulierung: hoechstens EIN kurzer Modellaufruf erkennt Absicht und Felder;
    gepruefte Werte laufen danach durch dieselbe lokale Anlage (handle), die nur Fehlendes erfragt.
    Kundendaten gehen nur an einen dafuer freigegebenen Anbieter - sonst Hinweis auf das Format."""
    import json

    from app.services.ai import router

    hint = ("Den Anlagewunsch habe ich nicht sicher erkannt. Bitte so eingeben: "
            "„Kunde Vorname Nachname anlegen, privat 0171 …, festnetz 02641 …“.")
    try:
        result = router.complete(system=_EXTRACT_SYSTEM, messages=[{"role": "user", "content": text}], user_id=user.id,
                                 tenant_id=user.tenant_id, customer_data=True, purpose="customer_create", max_tokens=200, max_calls=1)
    except router.AiError:
        return {"kind": "not_found", "message": hint}
    match = re.search(r"\{.*\}", result.text or "", re.S)
    try:
        data = json.loads(match.group(0)) if match else {}
    except ValueError:
        data = {}
    if not isinstance(data, dict) or data.get("anlegen") is not True:
        return {"kind": "not_found", "message": hint}

    def val(key, pattern=None):
        value = data.get(key)
        value = " ".join(value.split()) if isinstance(value, str) else ""
        return value if value and (pattern is None or re.fullmatch(pattern, value)) else ""

    phone = r"[+()0-9 /-]{6,30}"
    parts = ["Lege Kunden", val("vorname", r"[\wÄÖÜäöüß'’ -]{1,60}"), val("nachname", r"[\wÄÖÜäöüß'’ -]{1,60}"), "an"]
    for label, key in (("festnetz", "festnetz"), ("privat", "privat"), ("", "telefon_ohne_typ")):
        if val(key, phone):
            parts.append(f"{label} {val(key, phone)}".strip())
    street = val("strasse", r"[\wÄÖÜäöüß.' -]{2,80}")
    if street:
        parts.append(f"{street} {val('hausnummer', r'[0-9]{1,4}[a-zA-Z]?')}".strip())
    if val("plz", r"\d{5}"):
        parts.append(f"{val('plz')} {val('ort', r'[\wÄÖÜäöüß. -]{2,60}')}".strip())
    if val("email", r"[^@\s]+@[^@\s]+\.[a-zA-Z]{2,}"):
        parts.append(val("email"))
    return handle(user, " ".join(p for p in parts if p))


@dataclass
class Draft:
    first_name: str | None = None
    last_name: str | None = None
    phone_landline: str | None = None  # normalisiert "+49..."
    phone_private: str | None = None
    phones_open: list[str] = field(default_factory=list)  # ohne Stichwort - Typ wird erfragt
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
    def phones(self) -> list[str]:
        return [p for p in (self.phone_landline, self.phone_private, *self.phones_open) if p]

    @property
    def address(self) -> str | None:
        return " ".join(filter(None, [self.street, self.house_number])) or None

    def facts(self) -> list[str]:
        out = []
        if self.phone_landline:
            out.append(f"Festnetz {format_phone(self.phone_landline)}")
        if self.phone_private:
            out.append(f"Privattelefon {format_phone(self.phone_private)}")
        out.extend(f"Telefon {format_phone(p)} (Typ offen)" for p in self.phones_open)
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

    search_from = 0
    while (m := _PHONE.search(work, search_from)) is not None:
        candidate = _phone_candidate(m.group(0))
        if not candidate:
            search_from = m.end()
            continue
        # Ende der tatsaechlich verwendeten Ziffern im Original bestimmen (Rest wird erneut gesucht,
        # z. B. zwei Nummern direkt hintereinander).
        used, pos = 0, m.start()
        while pos < m.end() and used < len(candidate):
            if work[pos].isdigit() or work[pos] == "+":
                used += 1
            pos += 1
        end = pos
        normalized = normalize_phone(candidate)
        offset = max(0, m.start() - 30)
        label = _PHONE_KIND.search(work[offset:m.start()])
        kind = None if label is None else ("festnetz" if label.group(1).lower().startswith("fest") else "privat")
        if normalized in draft.phones:
            pass
        elif kind == "festnetz" and draft.phone_landline is None:
            draft.phone_landline = normalized
        elif kind == "privat" and draft.phone_private is None:
            draft.phone_private = normalized
        elif kind is None and len(draft.phones) < 2:
            draft.phones_open.append(normalized)
        else:
            draft.not_taken.append(format_phone(normalized))
        work = _mask(work, m.start(), end)
        if label is not None:  # Stichwort gilt nur fuer diese Nummer
            work = _mask(work, offset + label.start(1), offset + label.end(1))
        search_from = end

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


def apply_phone_kinds(draft: Draft, kinds) -> None:
    """Typen fuer Nummern ohne Stichwort (Reihenfolge wie im Text), z. B. ["festnetz", "privat"]."""
    if not draft.phones_open:
        return
    if not isinstance(kinds, list) or len(kinds) != len(draft.phones_open) or any(k not in PHONE_KINDS for k in kinds):
        raise CreateError("Bitte für jede Nummer Festnetz oder Privattelefon wählen.")
    for number, kind in zip(draft.phones_open, kinds):
        slot = "phone_landline" if kind == "festnetz" else "phone_private"
        if getattr(draft, slot) is not None:
            raise CreateError(f"{PHONE_KINDS[kind]} ist bereits vergeben.")
        setattr(draft, slot, number)
    draft.phones_open = []


def _phone_question(text: str, draft: Draft) -> dict:
    numbers = [format_phone(p) for p in draft.phones_open]
    free = [k for k, slot in (("festnetz", draft.phone_landline), ("privat", draft.phone_private)) if slot is None]
    if len(numbers) == 1:
        message = f"Ist {numbers[0]} Festnetz oder Privattelefon?"
        options = [{"kinds": [k], "label": PHONE_KINDS[k]} for k in free]
    else:
        message = "Welche Nummer ist Festnetz, welche Privattelefon?"
        options = [{"kinds": ["festnetz", "privat"], "label": f"Festnetz {numbers[0]}, privat {numbers[1]}"},
                   {"kinds": ["privat", "festnetz"], "label": f"Festnetz {numbers[1]}, privat {numbers[0]}"}]
    return {"kind": "create_phone", "message": message, "options": options, "create": _draft_payload(text, draft)}


def _conflicts(customer: Customer, draft: Draft) -> list[str]:
    """Angaben, die einem vorhandenen gleichnamigen Kunden widersprechen."""
    out = []
    keys = customer.phone_keys
    if draft.phones and keys and not set(draft.phones) & keys:
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
    if draft.phone_landline and not customer.phone_landline and draft.phone_landline != customer.phone_key:
        out.append("Festnetz")
    if draft.phone_private and not customer.phone_private and draft.phone_private != customer.phone_key:
        out.append("Privattelefon")
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


def handle(user, text: str, *, first_name=None, last_name=None, create_new: bool = False, phone_kinds=None) -> dict:
    from app.services.customer_notes import _choice

    draft = parse(text)
    if phone_kinds is not None:
        apply_phone_kinds(draft, phone_kinds)
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

    if draft.phones_open:  # Typ nicht raten - nachfragen (die Rueckfrage sendet denselben Text + Typen)
        return _phone_question(text, draft)

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
                create={"name": draft.name, "text": text, "first_name": draft.first_name, "last_name": draft.last_name,
                        "phone_kinds": phone_kinds},
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
            "phone_landline": format_phone(draft.phone_landline) if draft.phone_landline else None,
            "phone_private": format_phone(draft.phone_private) if draft.phone_private else None,
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
    keys = customer.phone_keys
    shared = Customer.query.filter(Customer.phone_key_in(keys), Customer.id != customer.id).limit(3).all() if keys else []
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

EDIT_FIELDS = ("first_name", "last_name", "name", "phone_landline", "phone_private", "phone", "street", "house_number", "address", "postal_code", "city", "email", "customer_number")


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
        "legacy_phone": bool(customer.phone),  # bisherige Nummer ohne Typ: nur anzeigen, wenn vorhanden
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
    for key, label in (("phone_landline", "Festnetznummer"), ("phone_private", "Privatnummer"), ("phone", "Telefonnummer")):
        if key not in data:  # nicht gesendet: unveraendert lassen (aeltere Schreibweisen nicht umformatieren)
            continue
        phone = text(key, 50)
        if phone and normalize_phone(phone) and phone == getattr(customer, key):
            continue
        if phone and not normalize_phone(phone):
            raise CreateError(f"Die {label} wurde nicht erkannt (mit Vorwahl angeben).")
        values[key] = format_phone(normalize_phone(phone)) if phone else None
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
