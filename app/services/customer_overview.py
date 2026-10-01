"""Zentriq-Kundenstamm: Suche, Leipziger-Vorgaenge und Memos je Kunde.

Gemeinsam genutzt von der Kundenliste (app/blueprints/customers) und der globalen Suche
(app/services/global_search.py). Alle Abfragen laufen ueber den globalen Tenant-Filter bzw.
sind zusaetzlich ausdruecklich auf den Mandanten beschraenkt - nie bueroubergreifend."""

from __future__ import annotations

from collections import defaultdict

from sqlalchemy import func, or_, select

from app.extensions import db
from app.models import Customer, CustomerMemo, Document, LeipzigerEntry
from app.services.leipziger_entries import search_key
from app.utils.customer_keys import normalize_customer_number

# Telefonnummern werden erst ab dieser Ziffernzahl gesucht (sonst trifft "12" fast alles).
MIN_PHONE_DIGITS = 5
# Vorgangsnummern erst ab dieser Laenge (Buchstaben/Ziffern), damit "12" nicht alles trifft.
MIN_CONTRACT_KEY_LENGTH = 4


def escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _digits(value: str) -> str:
    return "".join(ch for ch in value if ch.isdigit())


def phone_variants(digits: str) -> set[str]:
    """+49 341 ... und 0341 ... sollen sich gegenseitig finden."""
    variants = {digits}
    if digits.startswith("49") and len(digits) > 6:
        variants.add("0" + digits[2:])
    if digits.startswith("0049"):
        variants.add("0" + digits[4:])
    if digits.startswith("0") and not digits.startswith("00"):
        variants.add(digits[1:])
    return variants


def _normalized_phone_column():
    column = Customer.phone
    for char in (" ", "-", "/", "(", ")", ".", "+"):
        column = func.replace(column, char, "")
    return column


def customer_search_condition(query: str):
    """Treffer ueber Name, Telefonnummer (in jeder Schreibweise), Kundennummer und die
    Vertrags-/Vorgangsnummer eines zugeordneten Leipziger-Vorgangs."""
    like = f"%{escape_like(query)}%"
    conditions = [Customer.name.ilike(like, escape="\\")]
    number = normalize_customer_number(query)
    if number:
        conditions.append(Customer.customer_number_key.like(f"{escape_like(number)}%", escape="\\"))
    digits = _digits(query)
    if len(digits) >= MIN_PHONE_DIGITS:
        phone = _normalized_phone_column()
        conditions.extend(phone.like(f"%{variant}%") for variant in phone_variants(digits))
    contract = search_key(query)
    if len(contract) >= MIN_CONTRACT_KEY_LENGTH:
        conditions.append(
            Customer.id.in_(
                select(LeipzigerEntry.customer_id).where(
                    LeipzigerEntry.customer_id.is_not(None),
                    LeipzigerEntry.contract_key.like(f"%{escape_like(contract)}%", escape="\\"),
                )
            )
        )
    return or_(*conditions)


def _entry_identity(entry) -> tuple:
    """Derselbe Vorgang in mehreren Wochenlisten zaehlt einmal (Vertragsnummer, sonst
    Kunde + Sparte + Status)."""
    if entry.contract_key:
        return ("contract", entry.contract_key)
    return ("row", entry.customer_key, entry.product_line, entry.status_code, entry.start_date)


def customer_link_counts(customer_ids: list[int]) -> dict[int, dict[str, int]]:
    """Anzahl Leipziger-Vorgaenge (ohne Wiederholungen aus spaeteren Wochenlisten) und Memos je
    Kunde - zwei Abfragen fuer die ganze Seite."""
    counts = {customer_id: {"leipziger": 0, "memos": 0} for customer_id in customer_ids}
    if not customer_ids:
        return counts
    seen: dict[int, set] = defaultdict(set)
    rows = db.session.execute(
        select(
            LeipzigerEntry.customer_id,
            LeipzigerEntry.contract_key,
            LeipzigerEntry.customer_key,
            LeipzigerEntry.product_line,
            LeipzigerEntry.status_code,
            LeipzigerEntry.start_date,
        ).where(LeipzigerEntry.customer_id.in_(customer_ids))
    ).all()
    for row in rows:
        seen[row.customer_id].add(_entry_identity(row))
    for customer_id, identities in seen.items():
        counts[customer_id]["leipziger"] = len(identities)
    memo_rows = db.session.execute(
        select(CustomerMemo.customer_id, func.count(CustomerMemo.id))
        .where(CustomerMemo.customer_id.in_(customer_ids))
        .group_by(CustomerMemo.customer_id)
    ).all()
    for customer_id, count in memo_rows:
        counts[customer_id]["memos"] = count
    return counts


def customer_entries(customer: Customer) -> list[dict]:
    """Alle Leipziger-Vorgaenge des Kunden, neueste Liste zuerst; derselbe Vorgang aus
    mehreren Wochenlisten erscheint einmal (mit dem Stand der neuesten Liste)."""
    rows = db.session.execute(
        select(LeipzigerEntry, Document.original_filename, Document.uploaded_at)
        .join(Document, Document.id == LeipzigerEntry.document_id)
        .where(LeipzigerEntry.customer_id == customer.id, LeipzigerEntry.tenant_id == customer.tenant_id)
        .order_by(Document.uploaded_at.desc(), Document.id.desc(), LeipzigerEntry.position)
    ).all()
    result = []
    seen = set()
    for entry, filename, uploaded_at in rows:
        identity = _entry_identity(entry)
        if identity in seen:
            continue
        seen.add(identity)
        result.append({"entry": entry, "document_name": filename, "uploaded_at": uploaded_at})
    return result


def customer_memos(customer: Customer) -> list[CustomerMemo]:
    return (
        CustomerMemo.query.filter(CustomerMemo.customer_id == customer.id)
        .order_by(CustomerMemo.created_at.desc(), CustomerMemo.id.desc())
        .all()
    )
