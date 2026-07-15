"""Vertriebskennzahlen - reine Abfrage-/Berechnungsfunktionen, kein neuer State.
Naeherungswerte statt Euro-Betraege (siehe potential_score.py)."""

from collections import Counter

from app.models import Customer, Document, DocumentCustomer, Task
from app.models.enums import DocStatus, Priority, TaskStatus, WiedervorlageReason
from app.services.potential_score import compute_potential_score
from app.services.wiedervorlagen import get_open_offer_customer_dates


def _row_priority(row: dict) -> Priority:
    try:
        return Priority(row.get("priority", "medium"))
    except ValueError:
        return Priority.MEDIUM


def get_sales_kpis(user_id: int | None = None) -> dict:
    customer_ids: set[int] | None = None
    if user_id is not None:
        customer_ids = {c.id for c in Customer.query.filter_by(assigned_user_id=user_id).all()}

    angebot_customer_ids: set[int] = set()
    neugeschaeft_customer_ids: set[int] = set()
    sparten_counter: Counter[str] = Counter()
    potential_by_customer: dict[int, int] = {}

    for doc_customer in DocumentCustomer.query.all():
        if customer_ids is not None and doc_customer.customer_id not in customer_ids:
            continue
        for row in doc_customer.row_data or []:
            if row.get("is_angebot"):
                angebot_customer_ids.add(doc_customer.customer_id)
            if row.get("is_neugeschaeft"):
                neugeschaeft_customer_ids.add(doc_customer.customer_id)
            for product in row.get("products") or []:
                sparten_counter[product] += 1

            score = compute_potential_score(
                priority=_row_priority(row),
                products=row.get("products") or [],
                cross_sell_opportunity=bool(row.get("cross_sell_opportunity")),
                has_multiple_products=bool(row.get("has_multiple_products")),
            )
            potential_by_customer[doc_customer.customer_id] = max(
                potential_by_customer.get(doc_customer.customer_id, 0), score
            )

    abschlussquote = (
        round(len(neugeschaeft_customer_ids & angebot_customer_ids) / len(angebot_customer_ids) * 100, 1)
        if angebot_customer_ids
        else 0.0
    )

    document_query = Document.query.filter(Document.status == DocStatus.DONE, Document.processed_at.isnot(None))
    if user_id is not None:
        document_query = document_query.filter(Document.uploaded_by_user_id == user_id)
    durations_seconds = [
        (doc.processed_at - doc.uploaded_at).total_seconds()
        for doc in document_query.all()
        if doc.uploaded_at and doc.processed_at
    ]
    avg_processing_minutes = round(sum(durations_seconds) / len(durations_seconds) / 60, 1) if durations_seconds else 0.0

    open_offers = get_open_offer_customer_dates()
    if customer_ids is not None:
        open_offers = {cid: d for cid, d in open_offers.items() if cid in customer_ids}

    task_query = Task.query.filter(Task.status == TaskStatus.OPEN)
    if user_id is not None:
        task_query = task_query.filter(Task.assigned_user_id == user_id)
    offers_without_response = task_query.filter(
        Task.wiedervorlage_reason.in_(
            [WiedervorlageReason.OFFER_OLDER_THAN_14_DAYS, WiedervorlageReason.NO_RESPONSE]
        )
    ).count()

    cross_sell_query = Customer.query.join(Document, Document.customer_id == Customer.id).filter(
        Document.cross_sell_opportunity.is_(True)
    )
    if user_id is not None:
        cross_sell_query = cross_sell_query.filter(Customer.assigned_user_id == user_id)
    cross_sell_potential_count = cross_sell_query.distinct().count()

    top_customer_ids = sorted(potential_by_customer.items(), key=lambda item: item[1], reverse=True)[:5]
    top_customers = []
    for customer_id, score in top_customer_ids:
        customer = Customer.query.filter_by(id=customer_id).first()
        if customer is not None:
            top_customers.append({"customer": customer, "potential_score": score})

    return {
        "abschlussquote_percent": abschlussquote,
        "avg_processing_minutes": avg_processing_minutes,
        "open_offers_count": len(open_offers),
        "offers_without_response_count": offers_without_response,
        "vertraege_pro_sparte": dict(sparten_counter.most_common()),
        "top_customers": top_customers,
        "cross_sell_potential_count": cross_sell_potential_count,
    }
