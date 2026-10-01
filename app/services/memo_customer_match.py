"""Memo: moeglichen Kunden zu einem Transkript finden - deterministisch, ohne KI.

Reihenfolge: 1. genannte Kundennummer, 2. im Text genannte Telefonnummer, 3. Kundenname.
Ein Treffer nur ueber den Namen gilt nie als sicher (Status "possible").
Gesucht wird ausschliesslich im Kundenbestand des eigenen Bueros: alle Abfragen laufen ueber
den globalen Tenant-Filter (app/tenancy.py); ohne Tenant-Kontext schlaegt die Abfrage fehl.
Mehrere Treffer werden nie automatisch aufgeloest."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from sqlalchemy import or_

from app.models import Customer
from app.services.customer_normalization import normalize_customer_name
from app.utils.customer_keys import (  # noqa: F401 - auch von Tests/Schemas importiert
    format_phone,
    normalize_customer_number,
    normalize_phone,
)

MAX_CANDIDATES = 5
# Namensabgleich: hoechstens so viele Transkriptwoerter als Vorfilter, so viele Kandidaten.
MAX_NAME_PROBES = 40
MAX_NAME_ROWS = 2000

# Ziffernfolgen mit ueblichen Trennzeichen, optional mit +49 / 0049 / (0).
_PHONE_PATTERN = re.compile(r"(?<![\w+])(?:\+|00)?\d[\d \t\-/()]{4,22}\d(?!\w)")
_CUSTOMER_NUMBER_PATTERN = re.compile(
    r"(?:kunden|versicherungsnehmer|vn)\s*-?\s*(?:nummer|nr\.?)\s*(?:ist|lautet|:)?\s*([a-z0-9][a-z0-9\- /]{2,30}[a-z0-9])",
    re.IGNORECASE,
)


def extract_phone_numbers(text: str | None) -> list[str]:
    found: list[str] = []
    for match in _PHONE_PATTERN.finditer(text or ""):
        normalized = normalize_phone(match.group(0))
        if normalized and normalized not in found:
            found.append(normalized)
    return found


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
    return _load(Customer.query.filter(Customer.phone_key.in_(phones)))


def _by_customer_number(numbers: list[str]) -> list[Customer]:
    if not numbers:
        return []
    return _load(Customer.query.filter(Customer.customer_number_key.in_(numbers)))


def _by_name(text: str) -> list[Customer]:
    """Ein Kunde passt, wenn alle (mindestens zwei) Bestandteile seines Namens als Woerter im
    Transkript vorkommen - Reihenfolge egal ("Mustermann, Max" findet "Max Mustermann").
    Gelesen wird nur der vorberechnete Namensschluessel; Kandidaten werden in der Datenbank
    auf Namen vorgefiltert, die mindestens ein Wort des Transkripts enthalten."""
    words = {word for word in normalize_customer_name(text).split() if len(word) >= 2}
    if not words:
        return []
    # Laengste Woerter zuerst: Nachnamen sind selten, Fuellwoerter ("ist", "die") kurz.
    probes = sorted(words, key=len, reverse=True)[:MAX_NAME_PROBES]
    rows = (
        Customer.query.with_entities(Customer.id, Customer.name_key)
        .filter(Customer.name_key.isnot(None), or_(*(Customer.name_key.like(f"%{word}%") for word in probes)))
        .limit(MAX_NAME_ROWS)
        .all()
    )
    ids = [row.id for row in rows if (parts := row.name_key.split()) and len(parts) >= 2 and all(part in words for part in parts)]
    return _load(Customer.query.filter(Customer.id.in_(ids))) if ids else []


def _load(query) -> list[Customer]:
    return query.order_by(Customer.name, Customer.id).limit(MAX_CANDIDATES + 1).all()


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
