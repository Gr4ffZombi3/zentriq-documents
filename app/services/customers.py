from __future__ import annotations

import re
import unicodedata
from collections import defaultdict

from sqlalchemy import select
from sqlalchemy.orm import joinedload

from app.extensions import db
from app.models import Customer, DocumentCustomer, LeipzigerEntry
from app.services import customer_sources
from app.services.analysis.leipziger_liste_view import build_row_view
from app.services.customer_duplicates import duplicate_map, find_duplicates
from app.services.customer_overview import customer_link_counts, customer_search_condition
from app.services.leipziger_entries import search_key
from app.services.llm.schemas import ExtractedCustomer
from app.tenancy import get_current_tenant_id
from app.utils.customer_keys import customer_keys
from app.utils.vermittlernummer import format_vermittlernummer

DEFAULT_CUSTOMER_PAGE_SIZE = 25
MAX_CUSTOMER_PAGE_SIZE = 50
MIN_SEARCH_LENGTH = 2
MAX_SEARCH_LENGTH = 60


def normalize_customer_name(value: str | None) -> str:
    if not value:
        return ""
    replacements = {
        "ä": "ae",
        "ö": "oe",
        "ü": "ue",
        "ß": "ss",
        "Ä": "ae",
        "Ö": "oe",
        "Ü": "ue",
    }
    for source, target in replacements.items():
        value = value.replace(source, target)
    normalized = unicodedata.normalize("NFKD", value)
    without_accents = "".join(character for character in normalized if not unicodedata.combining(character))
    lowered = without_accents.lower()
    lowered = re.sub(r"[^a-z0-9\s]", " ", lowered)
    lowered = re.sub(r"\s+", " ", lowered)
    return lowered.strip()


def normalize_postal_code(value: str | None) -> str:
    if not value:
        return ""
    return re.sub(r"\s+", "", value).lower()


class CustomerMatcher:
    """Ordnet extrahierte Kundendaten (Leipziger Liste) dem Kundenstamm des eigenen Bueros zu.

    Abgleich ueber die indizierten Schluessel (app/utils/customer_keys.py) in dieser Reihenfolge:
    1. Kundennummer, 2. Telefonnummer, 3. dieselbe Vorgangsnummer aus einer frueheren Liste bei
    gleichem Namen (ohne abweichende PLZ), 4. Name + Geburtsdatum, 5. Name + PLZ (nur ohne
    abweichendes Geburtsdatum). Ein gleicher Name allein fuehrt nie zu einer Zuordnung, und
    abweichende Kundennummern schliessen eine Zuordnung immer aus. Gleicher Name und gleiche PLZ
    bei abweichendem Geburtsdatum ist unklar: es wird ein eigener Kunde angelegt und als
    moegliche Dublette zur Pruefung angezeigt (app/services/customer_duplicates.py). So legt eine neue
    Wochenliste fuer bereits bekannte Kunden keinen weiteren Datensatz an.
    Geladen werden nur Kunden des eigenen Bueros (globaler Tenant-Filter)."""

    def __init__(self, existing_customers: list[Customer] | None = None, *, source: str = customer_sources.LEIPZIGER_LISTE):
        customers = existing_customers or Customer.query.order_by(Customer.id.asc()).all()
        self.source = source
        self.customers: list[Customer] = []
        self._by_key: dict[str, dict[str, list[Customer]]] = {
            "customer_number_key": defaultdict(list),
            "phone_key": defaultdict(list),
            "name_key": defaultdict(list),
        }
        for customer in customers:
            self._index(customer, customer_keys(customer.name, customer.phone, customer.customer_number))
        self._contract_owner: dict[str, set[int]] | None = None

    def _contract_owners(self, contract_number: str | None) -> set[int]:
        """Kunden, denen dieselbe Vorgangsnummer in einer frueheren Liste zugeordnet war."""
        key = search_key(contract_number)[:100]
        if not key:
            return set()
        if self._contract_owner is None:
            self._contract_owner = defaultdict(set)
            rows = db.session.execute(
                select(LeipzigerEntry.contract_key, LeipzigerEntry.customer_id)
                .where(LeipzigerEntry.customer_id.is_not(None), LeipzigerEntry.contract_key.is_not(None))
                .distinct()
            ).all()
            for contract_key, customer_id in rows:
                self._contract_owner[contract_key].add(customer_id)
        return self._contract_owner.get(key, set())

    def _index(self, customer: Customer, keys: dict) -> None:
        if customer not in self.customers:
            self.customers.append(customer)
        for column, value in keys.items():
            if value and customer not in self._by_key[column][value]:
                self._by_key[column][value].append(customer)

    def find(self, data: ExtractedCustomer, contract_number: str | None = None) -> Customer | None:
        keys = customer_keys(data.name, data.phone, data.customer_number)
        number = keys["customer_number_key"]

        def number_conflict(customer: Customer) -> bool:
            other = customer_keys(None, None, customer.customer_number)["customer_number_key"]
            return bool(number and other and other != number)

        if number and (candidates := self._by_key["customer_number_key"].get(number)):
            return candidates[0]
        if keys["phone_key"]:
            candidates = [c for c in self._by_key["phone_key"].get(keys["phone_key"], []) if not number_conflict(c)]
            if candidates:
                return candidates[0]
        if not keys["name_key"]:
            return None
        same_name = [c for c in self._by_key["name_key"].get(keys["name_key"], []) if not number_conflict(c)]
        owners = self._contract_owners(contract_number)
        if owners:
            # Gleicher Name und dieselbe Vorgangsnummer wie in einer frueheren Liste: derselbe
            # Kunde - auch wenn das Geburtsdatum abweicht (typischer OCR-Lesefehler einer
            # frueheren Liste). Eine abweichende PLZ schliesst die Zuordnung aber aus.
            postal_code = normalize_postal_code(data.postal_code)
            matched = [
                c
                for c in same_name
                if c.id in owners
                and not (postal_code and c.postal_code and normalize_postal_code(c.postal_code) != postal_code)
            ]
            if len(matched) == 1:
                return matched[0]
        if data.date_of_birth is not None:
            matched = [c for c in same_name if c.date_of_birth == data.date_of_birth]
            if matched:
                return matched[0]
        postal_code = normalize_postal_code(data.postal_code)
        if postal_code:
            matched = [
                c
                for c in same_name
                if normalize_postal_code(c.postal_code) == postal_code
                and not (data.date_of_birth and c.date_of_birth and c.date_of_birth != data.date_of_birth)
            ]
            if matched:
                return matched[0]
        return None

    def get_or_create(
        self,
        data: ExtractedCustomer,
        uploaded_by_user_id: int | None = None,
        *,
        broker_number: str | None = None,
        contract_number: str | None = None,
    ) -> Customer:
        customer = self.find(data, contract_number)
        if customer is None:
            customer = Customer(
                name=data.name,
                tenant_id=get_current_tenant_id(),
                assigned_user_id=uploaded_by_user_id,
                source=self.source,
                field_sources={"name": self.source},
            )
            db.session.add(customer)

        # Die neueste Liste ist der aktuelle Stand; leere Quellwerte ueberschreiben nie
        # vorhandene Daten. Der Name bleibt unveraendert (Grundlage des Abgleichs).
        customer_sources.apply_customer_values(
            customer,
            {
                "address": data.address,
                "city": data.city,
                "postal_code": data.postal_code,
                "date_of_birth": data.date_of_birth,
                "phone": data.phone,
                "customer_number": data.customer_number,
                "broker_number": (format_vermittlernummer(broker_number) or "")[:50] or None,
            },
            self.source,
            overwrite=True,
        )
        self._index(customer, customer_keys(customer.name, customer.phone, customer.customer_number))
        return customer


def build_customer_directory(*, page: int = 1, per_page: int = DEFAULT_CUSTOMER_PAGE_SIZE, query: str = "") -> dict:
    """Kundenstamm des eigenen Bueros, optional gefiltert nach Name, Telefonnummer,
    Kundennummer oder Vorgangsnummer (gleiche Suche wie die globale Suche)."""
    safe_per_page = max(1, min(per_page, MAX_CUSTOMER_PAGE_SIZE))
    query = " ".join((query or "").split())[:MAX_SEARCH_LENGTH]
    statement = Customer.query.order_by(Customer.name.asc(), Customer.id.asc())
    if len(query) >= MIN_SEARCH_LENGTH:
        statement = statement.filter(customer_search_condition(query))
    pagination = statement.paginate(page=page, per_page=safe_per_page, error_out=False)

    customers = list(pagination.items)
    # Dubletten und Zaehler nur fuer die angezeigte Seite (je eine Abfrage).
    duplicates = duplicate_map(customers)
    counts = customer_link_counts([customer.id for customer in customers])
    items = [
        {
            "customer": customer,
            "leipziger_count": counts[customer.id]["leipziger"],
            "memo_count": counts[customer.id]["memos"],
            "possible_duplicates": duplicates.get(customer.id, []),
        }
        for customer in customers
    ]
    return {"items": items, "pagination": pagination, "query": query}


def build_customer_detail_context(customer: Customer) -> dict:
    document_customers = (
        DocumentCustomer.query.options(joinedload(DocumentCustomer.document))
        .filter(DocumentCustomer.customer_id == customer.id)
        .all()
    )
    document_customers.sort(
        key=lambda doc_customer: doc_customer.document.uploaded_at.timestamp() if doc_customer.document.uploaded_at else 0,
        reverse=True,
    )

    case_rows = []
    offers = 0
    closures = 0
    open_cases = 0
    stornos = 0
    for doc_customer in document_customers:
        confidence_rows = doc_customer.field_confidence or []
        for index, row in enumerate(doc_customer.row_data or []):
            confidence = confidence_rows[index] if index < len(confidence_rows) else {}
            row_view = build_row_view(doc_customer, row, confidence)
            case_rows.append(row_view)
            if row_view["status_key"] == "angebot":
                offers += 1
            if row_view["has_start_date"]:
                closures += 1
            if row_view["status_key"] == "storno":
                stornos += 1
            if not row_view["has_start_date"] and row_view["status_key"] != "storno":
                open_cases += 1

    return {
        "document_customers": document_customers,
        "case_rows": case_rows,
        "summary": {
            "documents": len({doc_customer.document_id for doc_customer in document_customers}),
            "cases": len(case_rows),
            "offers": offers,
            "closures": closures,
            "open_cases": open_cases,
            "stornos": stornos,
        },
        "possible_duplicates": find_duplicates(customer),
    }
