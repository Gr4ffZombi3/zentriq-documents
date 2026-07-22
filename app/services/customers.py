from __future__ import annotations

import re
import unicodedata
from collections import defaultdict
from datetime import date

from sqlalchemy.orm import joinedload, selectinload

from app.extensions import db
from app.models import Customer, DocumentCustomer
from app.services.analysis.leipziger_liste_view import build_row_view
from app.services.llm.schemas import ExtractedCustomer
from app.tenancy import get_current_tenant_id

DEFAULT_CUSTOMER_PAGE_SIZE = 25
MAX_CUSTOMER_PAGE_SIZE = 50


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


def _is_strong_customer_match(left: Customer, right: Customer) -> bool:
    if left.date_of_birth and right.date_of_birth and left.date_of_birth == right.date_of_birth:
        return True
    return bool(
        left.postal_code
        and right.postal_code
        and normalize_postal_code(left.postal_code) == normalize_postal_code(right.postal_code)
    )


class CustomerMatcher:
    def __init__(self, existing_customers: list[Customer] | None = None):
        customers = existing_customers or Customer.query.order_by(Customer.id.asc()).all()
        self.customers = list(customers)
        self.by_normalized_name: dict[str, list[Customer]] = defaultdict(list)
        for customer in self.customers:
            self.by_normalized_name[normalize_customer_name(customer.name)].append(customer)

    def get_or_create(self, data: ExtractedCustomer, uploaded_by_user_id: int | None = None) -> Customer:
        normalized_name = normalize_customer_name(data.name)
        candidates = self.by_normalized_name.get(normalized_name, [])

        matched = self._match_by_date_of_birth(candidates, data.date_of_birth)
        if matched is None:
            matched = self._match_by_postal_code(candidates, data.postal_code)

        customer = matched or Customer(
            name=data.name,
            tenant_id=get_current_tenant_id(),
            assigned_user_id=uploaded_by_user_id,
        )
        if matched is None:
            db.session.add(customer)
            self.customers.append(customer)
            self.by_normalized_name[normalized_name].append(customer)

        customer.address = data.address or customer.address
        customer.city = data.city or customer.city
        customer.postal_code = data.postal_code or customer.postal_code
        customer.date_of_birth = data.date_of_birth or customer.date_of_birth
        return customer

    @staticmethod
    def _match_by_date_of_birth(candidates: list[Customer], value: date | None) -> Customer | None:
        if value is None:
            return None
        return next((customer for customer in candidates if customer.date_of_birth == value), None)

    @staticmethod
    def _match_by_postal_code(candidates: list[Customer], value: str | None) -> Customer | None:
        normalized = normalize_postal_code(value)
        if not normalized:
            return None
        return next(
            (
                customer
                for customer in candidates
                if customer.postal_code and normalize_postal_code(customer.postal_code) == normalized
            ),
            None,
        )


def build_possible_duplicate_map(customers: list[Customer]) -> dict[int, list[Customer]]:
    grouped: dict[str, list[Customer]] = defaultdict(list)
    for customer in customers:
        grouped[normalize_customer_name(customer.name)].append(customer)

    duplicate_map: dict[int, list[Customer]] = {}
    for group in grouped.values():
        if len(group) < 2:
            continue
        for customer in group:
            matches = [candidate for candidate in group if candidate.id != customer.id and not _is_strong_customer_match(customer, candidate)]
            if matches:
                duplicate_map[customer.id] = matches
    return duplicate_map


def build_customer_directory(*, page: int = 1, per_page: int = DEFAULT_CUSTOMER_PAGE_SIZE) -> dict:
    safe_per_page = max(1, min(per_page, MAX_CUSTOMER_PAGE_SIZE))
    query = Customer.query.options(
        selectinload(Customer.document_customers).joinedload(DocumentCustomer.document)
    ).order_by(Customer.name.asc(), Customer.id.asc())
    pagination = query.paginate(page=page, per_page=safe_per_page, error_out=False)

    duplicate_map = build_possible_duplicate_map(Customer.query.order_by(Customer.id.asc()).all())
    items = [_build_customer_summary(customer, duplicate_map.get(customer.id, [])) for customer in pagination.items]
    return {"items": items, "pagination": pagination}


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

    duplicate_map = build_possible_duplicate_map(Customer.query.order_by(Customer.id.asc()).all())
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
        "possible_duplicates": duplicate_map.get(customer.id, []),
    }


def _build_customer_summary(customer: Customer, possible_duplicates: list[Customer]) -> dict:
    latest_row = None
    latest_uploaded_at = None
    record_count = 0

    for doc_customer in customer.document_customers:
        confidence_rows = doc_customer.field_confidence or []
        uploaded_at = doc_customer.document.uploaded_at
        for index, row in enumerate(doc_customer.row_data or []):
            record_count += 1
            if latest_uploaded_at is None or (uploaded_at and uploaded_at > latest_uploaded_at):
                confidence = confidence_rows[index] if index < len(confidence_rows) else {}
                latest_uploaded_at = uploaded_at
                latest_row = build_row_view(doc_customer, row, confidence)

    return {
        "customer": customer,
        "record_count": record_count,
        "latest_row": latest_row,
        "latest_uploaded_at": latest_uploaded_at,
        "possible_duplicates": possible_duplicates,
        "status_label": latest_row["result_label"] if latest_row else "Noch keine Vorgänge",
    }
