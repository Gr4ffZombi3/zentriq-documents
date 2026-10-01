"""Globale Suche in der Kopfleiste: Kunden und Vorgaenge der Leipziger Liste.

Rechte (serverseitig, zusaetzlich zur Mandantentrennung ueber den globalen Tenant-Filter):
- OFFICE_ADMIN: Kunden (Name, Telefon, Kundennummer) und alle Vorgaenge der aktuellen Liste
  des eigenen Bueros.
- EMPLOYEE: ausschliesslich Vorgaenge der aktuellen Liste mit der eigenen Vermittlernummer -
  keine Kundenstammdaten (wie bisher).
- SUPER_ADMIN: keine Ergebnisse (der Endpunkt ist fuer ihn ohnehin gesperrt).

Memos werden nicht gespeichert (app/services/memo.py) und sind daher nicht durchsuchbar.
Jede Gruppe ist auf wenige Treffer begrenzt; es werden nur die angezeigten Spalten gelesen."""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import func, or_, select

from app.extensions import db
from app.models import Customer, Document, LeipzigerEntry
from app.models.enums import DocStatus, DocType
from app.services.leipziger_entries import search_key
from app.utils.vermittlernummer import vermittlernummer_key

MIN_QUERY_LENGTH = 2
MAX_QUERY_LENGTH = 60
GROUP_LIMIT = 20
# Telefonnummern werden erst ab dieser Ziffernzahl gesucht (sonst trifft "12" fast alles).
MIN_PHONE_DIGITS = 5


@dataclass
class SearchResults:
    query: str
    customers: list = field(default_factory=list)
    customers_more: bool = False
    entries: list = field(default_factory=list)
    entries_more: bool = False
    document: Document | None = None
    show_customers: bool = False

    @property
    def total(self) -> int:
        return len(self.customers) + len(self.entries)


def normalize_query(raw: str | None) -> str:
    return " ".join((raw or "").split())[:MAX_QUERY_LENGTH]


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _digits(value: str) -> str:
    return "".join(ch for ch in value if ch.isdigit())


def _phone_variants(digits: str) -> set[str]:
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


def current_document() -> Document | None:
    """Aktuelle (neueste fertig ausgewertete) Leipziger Liste des eigenen Bueros."""
    return db.session.execute(
        select(Document)
        .where(Document.doc_type == DocType.LEIPZIGER_LISTE, Document.status == DocStatus.DONE)
        .order_by(Document.uploaded_at.desc(), Document.id.desc())
        .limit(1)
    ).scalar_one_or_none()


def search_customers(query: str) -> tuple[list, bool]:
    like = f"%{_escape_like(query)}%"
    conditions = [
        Customer.name.ilike(like, escape="\\"),
        Customer.customer_number.ilike(f"{_escape_like(query)}%", escape="\\"),
    ]
    digits = _digits(query)
    if len(digits) >= MIN_PHONE_DIGITS:
        phone = _normalized_phone_column()
        conditions.extend(phone.like(f"%{variant}%") for variant in _phone_variants(digits))
    rows = db.session.execute(
        select(Customer.id, Customer.name, Customer.customer_number, Customer.phone, Customer.postal_code, Customer.city)
        .where(or_(*conditions))
        .order_by(Customer.name, Customer.id)
        .limit(GROUP_LIMIT + 1)
    ).all()
    return rows[:GROUP_LIMIT], len(rows) > GROUP_LIMIT


def search_entries(query: str, document: Document, broker_key: str | None, *, all_brokers: bool) -> tuple[list, bool]:
    key = search_key(query)
    if not key or (not all_brokers and not broker_key):
        return [], False
    like = f"%{key}%"
    conditions = [LeipzigerEntry.contract_key.like(like), LeipzigerEntry.customer_key.like(like)]
    if all_brokers:
        conditions.append(LeipzigerEntry.broker_key.like(f"{key}%"))
    statement = select(
        LeipzigerEntry.contract_number,
        LeipzigerEntry.customer_name,
        LeipzigerEntry.broker_number,
        LeipzigerEntry.broker_key,
        LeipzigerEntry.status_code,
        LeipzigerEntry.product_line,
        LeipzigerEntry.start_date,
    ).where(
        LeipzigerEntry.tenant_id == document.tenant_id,
        LeipzigerEntry.document_id == document.id,
        or_(*conditions),
    )
    if not all_brokers:
        statement = statement.where(LeipzigerEntry.broker_key == broker_key)
    rows = db.session.execute(statement.order_by(LeipzigerEntry.position, LeipzigerEntry.id).limit(GROUP_LIMIT + 1)).all()
    return rows[:GROUP_LIMIT], len(rows) > GROUP_LIMIT


def global_search(user, raw_query: str | None) -> SearchResults:
    query = normalize_query(raw_query)
    is_office_admin = bool(user.is_office_admin)
    results = SearchResults(query=query, show_customers=is_office_admin)
    if len(query) < MIN_QUERY_LENGTH or not (is_office_admin or user.is_employee):
        return results

    if is_office_admin:
        results.customers, results.customers_more = search_customers(query)

    results.document = current_document()
    if results.document is not None:
        results.entries, results.entries_more = search_entries(
            query,
            results.document,
            vermittlernummer_key(user.vermittlernummer),
            all_brokers=is_office_admin,
        )
    return results
