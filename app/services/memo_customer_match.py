"""Memo: moeglichen Kunden zu einem Transkript finden - deterministisch, ohne KI.

Reihenfolge: 1. genannte Kundennummer, 2. im Text genannte Telefonnummer, 3. Kundenname.
Ein Treffer nur ueber den Namen gilt nie als sicher (Status "possible").
Gesucht wird ausschliesslich im Kundenbestand des eigenen Bueros: alle Abfragen laufen ueber
den globalen Tenant-Filter (app/tenancy.py); ohne Tenant-Kontext schlaegt die Abfrage fehl.
Mehrere Treffer werden nie automatisch aufgeloest."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.models import Customer
from app.services.customer_normalization import normalize_customer_name

MAX_CANDIDATES = 5

# Ziffernfolgen mit ueblichen Trennzeichen, optional mit +49 / 0049 / (0).
_PHONE_PATTERN = re.compile(r"(?<![\w+])(?:\+|00)?\d[\d \t\-/()]{4,22}\d(?!\w)")
_CUSTOMER_NUMBER_PATTERN = re.compile(
    r"(?:kunden|versicherungsnehmer|vn)\s*-?\s*(?:nummer|nr\.?)\s*(?:ist|lautet|:)?\s*([a-z0-9][a-z0-9\- /]{2,30}[a-z0-9])",
    re.IGNORECASE,
)


def normalize_phone(raw: str | None) -> str | None:
    """Einheitliche Form "+49..." fuer deutsche Schreibweisen:
    0171 1234567, 01711234567, +49 171 1234567, 0049 171 1234567, +49 (0)171 1234567."""
    if not raw:
        return None
    text = raw.strip()
    has_plus = text.startswith("+")
    digits = re.sub(r"\D", "", text)
    if has_plus:
        pass
    elif digits.startswith("00"):
        digits = digits[2:]
    elif digits.startswith("0"):
        digits = "49" + digits[1:]
    else:
        # Ohne Vorwahl ist eine Nummer nicht eindeutig zuzuordnen.
        return None
    if digits.startswith("490"):
        digits = "49" + digits[3:]
    if not 9 <= len(digits) <= 15:
        return None
    return "+" + digits


def format_phone(normalized: str) -> str:
    """Lesbare Anzeige deutscher Nummern als 0171 1234567."""
    if normalized.startswith("+49"):
        national = "0" + normalized[3:]
        return f"{national[:4]} {national[4:]}" if national.startswith("01") else national
    return normalized


def extract_phone_numbers(text: str | None) -> list[str]:
    found: list[str] = []
    for match in _PHONE_PATTERN.finditer(text or ""):
        normalized = normalize_phone(match.group(0))
        if normalized and normalized not in found:
            found.append(normalized)
    return found


def normalize_customer_number(raw: str | None) -> str:
    return re.sub(r"[^0-9A-Z]", "", (raw or "").upper())


def extract_customer_numbers(text: str | None) -> list[str]:
    found: list[str] = []
    for match in _CUSTOMER_NUMBER_PATTERN.finditer(text or ""):
        value = normalize_customer_number(match.group(1))
        if len(value) >= 3 and value not in found:
            found.append(value)
    return found


@dataclass
class MatchResult:
    status: str  # "unique" | "possible" (nur Name, ein Kandidat) | "multiple" | "none"
    matched_by: str | None = None  # "phone" | "customer_number" | "name"
    customers: list[Customer] = field(default_factory=list)
    phones: list[str] = field(default_factory=list)
    customer_numbers: list[str] = field(default_factory=list)


def _by_phone(phones: list[str]) -> list[Customer]:
    if not phones:
        return []
    wanted = set(phones)
    rows = Customer.query.with_entities(Customer.id, Customer.phone).filter(Customer.phone.isnot(None)).all()
    ids = [row.id for row in rows if normalize_phone(row.phone) in wanted]
    return _load(ids)


def _by_customer_number(numbers: list[str]) -> list[Customer]:
    if not numbers:
        return []
    wanted = set(numbers)
    rows = (
        Customer.query.with_entities(Customer.id, Customer.customer_number)
        .filter(Customer.customer_number.isnot(None))
        .all()
    )
    ids = [row.id for row in rows if normalize_customer_number(row.customer_number) in wanted]
    return _load(ids)


def _by_name(text: str) -> list[Customer]:
    """Ein Kunde passt, wenn alle (mindestens zwei) Bestandteile seines Namens als Woerter im
    Transkript vorkommen - Reihenfolge egal ("Mustermann, Max" findet "Max Mustermann")."""
    words = set(normalize_customer_name(text).split())
    if not words:
        return []
    ids = []
    for row in Customer.query.with_entities(Customer.id, Customer.name).all():
        parts = [part for part in normalize_customer_name(row.name).split() if len(part) >= 2]
        if len(parts) >= 2 and all(part in words for part in parts):
            ids.append(row.id)
    return _load(ids)


def _load(ids: list[int]) -> list[Customer]:
    if not ids:
        return []
    return Customer.query.filter(Customer.id.in_(ids[: MAX_CANDIDATES + 1])).order_by(Customer.name).all()


def match_customer(transcript: str | None) -> MatchResult:
    text = (transcript or "").strip()
    phones = extract_phone_numbers(text)
    numbers = extract_customer_numbers(text)
    result = MatchResult(status="none", phones=phones, customer_numbers=numbers)
    if not text:
        return result
    for matched_by, finder, argument in (
        ("customer_number", _by_customer_number, numbers),
        ("phone", _by_phone, phones),
        ("name", _by_name, text),
    ):
        candidates = finder(argument)
        if candidates:
            result.matched_by = matched_by
            result.customers = candidates[:MAX_CANDIDATES]
            if len(candidates) > 1:
                result.status = "multiple"
            else:
                result.status = "possible" if matched_by == "name" else "unique"
            return result
    return result
