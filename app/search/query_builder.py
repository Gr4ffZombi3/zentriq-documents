"""Uebersetzt eine whitelist-typisierte FilterSpec in eine reine SQLAlchemy-ORM-Query.
Die KI (search_parser.py) waehlt nur, WELCHE dieser vordefinierten Filter angewendet werden -
sie generiert niemals SQL selbst. Unbekannte/nicht-whitelisted Felder werden von pydantic
automatisch verworfen (Standardverhalten von BaseModel)."""

from datetime import date

from pydantic import BaseModel

from app.extensions import db
from app.models import Customer, Document
from app.models.enums import DocType, Priority


class FilterSpec(BaseModel):
    doc_type: DocType | None = None
    city: str | None = None
    postal_code: str | None = None
    has_product: str | None = None
    missing_product: str | None = None
    is_neugeschaeft: bool | None = None
    is_fahrzeugwechsel: bool | None = None
    priority: Priority | None = None
    customer_name_contains: str | None = None
    date_from: date | None = None
    date_to: date | None = None


def search_documents(filter_spec: FilterSpec) -> list[Document]:
    query = Document.query.outerjoin(Customer, Document.customer_id == Customer.id)

    if filter_spec.doc_type is not None:
        query = query.filter(Document.doc_type == filter_spec.doc_type)
    if filter_spec.city:
        query = query.filter(Customer.city.ilike(f"%{filter_spec.city}%"))
    if filter_spec.postal_code:
        query = query.filter(Customer.postal_code == filter_spec.postal_code)
    if filter_spec.is_neugeschaeft is not None:
        query = query.filter(Document.is_neugeschaeft == filter_spec.is_neugeschaeft)
    if filter_spec.is_fahrzeugwechsel is not None:
        query = query.filter(Document.is_fahrzeugwechsel == filter_spec.is_fahrzeugwechsel)
    if filter_spec.priority is not None:
        query = query.filter(Document.priority == filter_spec.priority)
    if filter_spec.customer_name_contains:
        query = query.filter(Customer.name.ilike(f"%{filter_spec.customer_name_contains}%"))
    if filter_spec.date_from:
        query = query.filter(Document.contract_start_date >= filter_spec.date_from)
    if filter_spec.date_to:
        query = query.filter(Document.contract_start_date <= filter_spec.date_to)

    documents = query.order_by(Document.uploaded_at.desc()).all()

    # Produktlisten sind ein JSON-Feld ohne portable DB-seitige Contains-Abfrage
    # (SQLite/MariaDB); daher wird hier sicher in Python nachgefiltert statt roh-SQL zu bauen.
    if filter_spec.has_product:
        target = filter_spec.has_product.lower()
        documents = [d for d in documents if any(target in p.lower() for p in (d.products or []))]
    if filter_spec.missing_product:
        target = filter_spec.missing_product.lower()
        documents = [d for d in documents if not any(target in p.lower() for p in (d.products or []))]

    return documents


def fallback_text_search(query: str) -> list[Document]:
    like = f"%{query}%"
    return (
        Document.query.outerjoin(Customer, Document.customer_id == Customer.id)
        .filter(
            db.or_(
                Customer.name.ilike(like),
                Customer.city.ilike(like),
                Document.raw_text.ilike(like),
            )
        )
        .order_by(Document.uploaded_at.desc())
        .all()
    )
