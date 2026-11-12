"""Kunde anlegen ueber die Maske (Kundenverwaltung > "Kunde anlegen").

1. parse_block: eingefuegter Text (Adressblock, E-Mail-Abschnitt mit Signatur, eine Zeile mit Kommas)
   -> erkannte Felder der vorhandenen Kundenmaske. Lokal und sofort (keine KI): Anrede, Grussformel und
   Fliesstext werden verworfen, Strasse/PLZ-Ort/Telefon/E-Mail ueber ihr Format erkannt, der Name nur aus
   einer Zeile, die eindeutig ein Name ist (nie eine Strasse). Unklares bleibt leer. Speichert nichts.
2. clean_values: dieselben Pruefregeln wie beim Bearbeiten (customer_create.update_fields).
3. duplicates: konkreter Dublettenverdacht im eigenen Buero (gleicher Name, Telefon, E-Mail, VN-Nummer).
4. create: legt den Kunden an (Kundensperre des Bueros, erneute Pruefung) - bei Verdacht nur mit
   ausdruecklichem "trotzdem anlegen"."""

from __future__ import annotations

import re

from app.extensions import db
from app.models import Customer
from app.services import customer_sources
from app.services.customer_create import _EMAIL, CreateError, _cap, _clean_name_part
from app.services.customer_lock import CustomerLockTimeout, customer_creation_lock, fresh_transaction
from app.utils.customer_keys import format_phone, name_key, normalize_phone, vn_key

FIELDS = ("first_name", "last_name", "street", "house_number", "postal_code", "city", "phone_landline", "phone_private",
          "email", "customer_number")
MAX_TEXT = 5000

_GREETING = re.compile(
    r"^\s*(?:hallo|hi|liebe[rs]?|sehr\s+geehrte[rs]?|guten\s+(?:tag|morgen|abend)|moin|servus|"
    r"mit\s+freundlichen\s+gr(?:ü|ue)(?:ß|ss)en|freundliche\s+gr(?:ü|ue)(?:ß|ss)e|viele\s+gr(?:ü|ue)(?:ß|ss)e|beste\s+gr(?:ü|ue)(?:ß|ss)e|"
    r"liebe\s+gr(?:ü|ue)(?:ß|ss)e|herzliche\s+gr(?:ü|ue)(?:ß|ss)e|gr(?:ü|ue)(?:ß|ss)e|lg|vg|mfg|danke|vielen\s+dank|"
    r"von\s+meinem\s+iphone|gesendet\s+von|von:|an:|betreff:|gesendet:|datum:|cc:)\b",
    re.I,
)
_CLOSING = re.compile(r"^\s*(?:mit\s+freundlichen|freundliche|viele|beste|liebe|herzliche)?\s*gr(?:ü|ue)(?:ß|ss)(?:e|en)\b|^\s*(?:mfg|lg|vg)\b", re.I)
_PLZ_CITY = re.compile(r"^(?:D\s*-\s*)?(\d{5})\s+([A-ZÄÖÜa-zäöüß][\wÄÖÜäöüß .()'/-]{1,60})$")
_STREET = re.compile(
    r"^([A-ZÄÖÜa-zäöüß][\wÄÖÜäöüß.' -]{1,80}?)\s+(\d{1,4}\s*[a-zA-Z]?(?:\s*[-/]\s*\d{1,4}\s*[a-zA-Z]?)?)$"
)
_PHONE_LINE = re.compile(r"(?:(tel(?:efon)?|fon|festnetz|büro|buero|privat|mobil|handy|mobile|cell|fax)\.?\s*[:.]?\s*)?(\+?[\d][\d /().-]{5,24}\d)", re.I)
_MOBILE_LABEL = re.compile(r"mobil|handy|mobile|cell|privat", re.I)
_SALUTATION = re.compile(r"^(?:herrn?|frau|hr\.|fr\.|dr\.|prof\.)\s+", re.I)
_NAME_WORD = re.compile(r"[A-ZÄÖÜ][a-zäöüßéèáóúç]+(?:[-'][A-ZÄÖÜ]?[a-zäöüßéèáóúç]+)*|von|van|de|der|zu|vom|den|ten|ter")
_NOT_NAME_WORDS = {
    "angebot", "versicherung", "vertrag", "kfz", "huk", "termin", "daten", "anbei", "gmbh", "ag", "kg", "ohg", "ug", "mbh",
    "e.v.", "büro", "buero", "telefon", "mobil", "fax", "email", "e-mail", "mail", "www", "straße", "strasse", "deutschland",
}


def _lines(text: str) -> list[str]:
    raw = (text or "")[:MAX_TEXT].replace("\r", "\n")
    lines = []
    for line in raw.split("\n"):
        line = line.strip().strip("|>").strip()
        if not line:
            continue
        # eine Zeile mit Kommas/Semikolons ("53111 Bonn, Hauptstr. 3, Peter Krause") in Teile zerlegen
        parts = [p.strip() for p in re.split(r"\s*[;,]\s*|\s{3,}|\t", line) if p.strip()]
        lines.extend(parts if len(parts) > 1 else [line])
    return lines


def _is_name(line: str) -> tuple[str, str] | None:
    """Zeile ist eindeutig ein Personenname ("Max Mustermann", "Herr Dr. Max von Muster")."""
    text = _SALUTATION.sub("", _SALUTATION.sub("", line.strip(" .:"))).strip()
    # Ziffern/@ (Strasse mit Hausnummer, Telefon, E-Mail) bzw. ausdrueckliches Strassenwort -> kein Name.
    # Endungen wie "-berg" sind bei Nachnamen haeufig (Weinberg) und schliessen nichts aus.
    if not text or re.search(r"[\d@/]", text) or re.search(r"stra(?:ß|ss)e\b|\bstr\.", text, re.I):
        return None
    words = text.split()
    if not 2 <= len(words) <= 4 or any(w.lower().strip(".,") in _NOT_NAME_WORDS for w in words):
        return None
    if not all(_NAME_WORD.fullmatch(w) for w in words):
        return None
    if words[0].lower() in {"von", "van", "de", "der", "zu", "vom", "den", "ten", "ter"}:
        return None
    first, last = _clean_name_part(words[0]), _clean_name_part(" ".join(words[1:]))
    return (first, last) if first and last else None


def parse_block(text: str) -> dict:
    """Erkannte Felder (nur eindeutige; Rest bleibt leer). Speichert nichts."""
    found: dict[str, str] = {}
    lines = _lines(text)
    email = re.search(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", text or "")
    if email:
        found["email"] = email.group(0).lower()
    vn = re.search(r"\b\d{3}\s*/\s*\d{6}\s*-?\s*[A-Za-z]\b", text or "")
    if vn and vn_key(vn.group(0)):
        key = vn_key(vn.group(0))
        found["customer_number"] = f"{key[:3]}/{key[3:9]}-{key[9]}"

    name_candidates: list[tuple[int, tuple[str, str]]] = []
    street_at = plz_at = None
    after_closing = None
    for i, line in enumerate(lines):
        if _CLOSING.match(line):
            after_closing = i
            continue
        if _GREETING.match(line) or "@" in line or (vn and vn.group(0) in line):
            continue
        if len(line.split()) > 7:  # Fliesstext
            continue
        m = _PLZ_CITY.match(line)
        if m and "postal_code" not in found:
            found["postal_code"], found["city"] = m.group(1), _cap_city(m.group(2))
            plz_at = i
            continue
        phone = _PHONE_LINE.fullmatch(line) or (_PHONE_LINE.search(line) if re.match(r"^\s*(?:tel|fon|festnetz|mobil|handy|privat|büro|buero)", line, re.I) else None)
        if phone:
            label = phone.group(1) or ""
            number = normalize_phone(phone.group(2))
            if number and not label.lower().startswith("fax"):
                mobile = bool(_MOBILE_LABEL.search(label)) or (not label and number.startswith("+491"))
                key = "phone_private" if mobile else "phone_landline"
                found.setdefault(key, format_phone(number))
            continue
        m = _STREET.match(line)
        if m and "street" not in found:  # Text + Hausnummer am Ende ist eine Strasse ("Am Hang 5a"), nie ein Name
            found["street"], found["house_number"] = m.group(1).strip(), re.sub(r"\s+", "", m.group(2))
            street_at = i
            continue
        name = _is_name(line)
        if name:
            name_candidates.append((i, name))

    # Name: bevorzugt direkt ueber der Strasse (Adressblock) bzw. direkt nach der Grussformel (Signatur);
    # mehrere gleichwertige Kandidaten -> unklar, leer lassen.
    chosen = None
    for anchor in (street_at, plz_at):
        if anchor is not None:
            chosen = next((n for i, n in name_candidates if i == anchor - 1), None)
            if chosen:
                break
    if chosen is None and after_closing is not None:
        chosen = next((n for i, n in name_candidates if i == after_closing + 1), None)
    if chosen is None and len(name_candidates) == 1:
        chosen = name_candidates[0][1]
    if chosen:
        found["first_name"], found["last_name"] = chosen
    return {k: v for k, v in found.items() if v}


def _cap_city(value: str) -> str:
    value = " ".join(value.split())
    return value if not (value.islower() or value.isupper()) else " ".join(_cap(w) for w in value.split())


# --- Pruefen und Anlegen ---------------------------------------------------------------------------


def clean_values(data: dict) -> dict:
    """Dieselben Regeln wie die Kundenmaske beim Bearbeiten. Pflicht: Vor- und Nachname."""
    def text(key, limit):
        value = data.get(key)
        value = " ".join(value.split()) if isinstance(value, str) else ""
        if len(value) > limit:
            raise CreateError("Eine Angabe ist zu lang.")
        return value or None

    first, last = _clean_name_part(data.get("first_name") or ""), _clean_name_part(data.get("last_name") or "")
    if not first or not last:
        raise CreateError("Bitte Vor- und Nachnamen angeben.")
    values = {"first_name": first, "last_name": last, "name": f"{first} {last}"}
    for key, label in (("phone_landline", "Festnetznummer"), ("phone_private", "Privatnummer")):
        phone = text(key, 50)
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
    street, number = text("street", 200), text("house_number", 20)
    values.update(street=street, house_number=number, address=" ".join(filter(None, [street, number])) or None)
    vn = text("customer_number", 50)
    if vn:
        key = vn_key(vn)
        if key is None:
            raise CreateError("Bitte eine gültige Versicherungsnehmernummer angeben (z. B. 808/123456-E).")
        vn = f"{key[:3]}/{key[3:9]}-{key[9]}"
    values["customer_number"] = vn
    return {k: v for k, v in values.items() if v}


def duplicates(values: dict) -> list[tuple[Customer, str]]:
    """Konkreter Dublettenverdacht im eigenen Buero (Tenant-Filter): (Kunde, Grund)."""
    from app.services.customer_notes import customers_by_vn

    found: dict[int, tuple[Customer, str]] = {}

    def add(customers, reason):
        for customer in customers:
            found.setdefault(customer.id, (customer, reason))

    if values.get("customer_number"):
        ids = customers_by_vn(vn_key(values["customer_number"]))
        add(Customer.query.filter(Customer.id.in_(ids)).all() if ids else [], "gleiche VN-Nummer")
    phones = [normalize_phone(values[k]) for k in ("phone_landline", "phone_private") if values.get(k)]
    phones = [p for p in phones if p]
    if phones:
        add(Customer.query.filter(Customer.phone_key_in(phones)).limit(5).all(), "gleiche Telefonnummer")
    if values.get("email"):
        add(Customer.query.filter(db.func.lower(Customer.email) == values["email"].lower()).limit(5).all(), "gleiche E-Mail-Adresse")
    key = name_key(values.get("name"))
    if key:
        add(Customer.query.filter(Customer.name_key == key).limit(5).all(), "gleicher Name")
    return list(found.values())[:6]


def serialize_duplicate(customer: Customer, reason: str) -> dict:
    from flask import url_for

    place = " ".join(filter(None, [customer.postal_code, customer.city])) or None
    detail = " · ".join(filter(None, [customer.customer_number, customer.address, place, customer.phone_display]))
    return {"id": customer.id, "name": customer.name, "reason": reason, "detail": detail,
            "url": url_for("customers.detail", customer_id=customer.id)}


def create(user, values: dict, *, allow_duplicate: bool = False) -> tuple[Customer | None, list[tuple[Customer, str]]]:
    """(neuer Kunde, []) oder (None, Dubletten) bei Verdacht ohne ausdrueckliches "trotzdem anlegen"."""
    lock = customer_creation_lock(user.tenant_id, timeout_seconds=20)
    try:
        lock.acquire()
    except CustomerLockTimeout:
        raise CreateError("Gerade werden Kundendaten gespeichert. Bitte in einigen Sekunden erneut versuchen.", 409) from None
    try:
        fresh_transaction()
        found = duplicates(values)  # erneut unter der Sperre (zweiter Mitarbeiter, Doppelklick)
        vn_owner = any(reason == "gleiche VN-Nummer" for _c, reason in found)
        if vn_owner:
            raise CreateError("Diese Versicherungsnehmernummer gehört bereits zu einem anderen Kunden.", 409)
        if found and not allow_duplicate:
            return None, found
        customer = Customer(
            tenant_id=user.tenant_id,
            assigned_user_id=user.id,
            source=customer_sources.MANUELL,
            field_sources={k: customer_sources.MANUELL for k in values},
            **values,
        )
        db.session.add(customer)
        db.session.commit()
    finally:
        lock.release()
    from app.models.audit_log import AuditEventType
    from app.services.audit import log_audit_event

    log_audit_event(AuditEventType.CUSTOMER_CREATED, user=user, details={"customer_id": customer.id, "source": customer_sources.MANUELL})
    return customer, []
