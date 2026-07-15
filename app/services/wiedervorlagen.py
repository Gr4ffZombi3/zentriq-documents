"""Erkennt automatisch offene Angebote, die 7 bzw. 14 Tage alt sind, und erzeugt/eskaliert
dafuer eine Wiedervorlage-Aufgabe. Wird synchron beim Laden von Tagescockpit/Mein Bestand
aufgerufen (kein Celery-Beat noetig) und ist idempotent: bereits offene Wiedervorlagen
werden wiederverwendet/eskaliert statt dupliziert."""

from datetime import date

from app.extensions import db
from app.models import Customer, Document, DocumentCustomer, Task
from app.models.enums import Priority, TaskStatus, TaskType, TimelineEventType, WiedervorlageReason
from app.services.timeline import log_timeline_event
from app.tenancy import get_current_tenant_id

WIEDERVORLAGE_LABELS: dict[WiedervorlageReason, str] = {
    WiedervorlageReason.OFFER_OLDER_THAN_7_DAYS: "📧 Angebot nachfassen (älter als 7 Tage)",
    WiedervorlageReason.OFFER_OLDER_THAN_14_DAYS: "📧 Angebot dringend nachfassen (älter als 14 Tage)",
}

OFFER_AGE_REASONS = (WiedervorlageReason.OFFER_OLDER_THAN_7_DAYS, WiedervorlageReason.OFFER_OLDER_THAN_14_DAYS)


def _earliest_open_offer_dates() -> dict[int, date]:
    """customer_id -> Datum der aeltesten Dokument-Zeile mit is_angebot=True, sofern dieser
    Kunde nicht spaeter (nach diesem Angebot) bereits als Neugeschaeft erkannt wurde."""
    earliest_offer: dict[int, date] = {}
    closed_customer_ids: set[int] = set()

    doc_customers = (
        DocumentCustomer.query.join(Document, DocumentCustomer.document_id == Document.id)
        .order_by(Document.uploaded_at.asc())
        .all()
    )
    for doc_customer in doc_customers:
        rows = doc_customer.row_data or []
        uploaded_at = doc_customer.document.uploaded_at.date()
        if any(row.get("is_neugeschaeft") for row in rows):
            closed_customer_ids.add(doc_customer.customer_id)
        if any(row.get("is_angebot") for row in rows) and doc_customer.customer_id not in earliest_offer:
            earliest_offer[doc_customer.customer_id] = uploaded_at

    return {
        customer_id: offer_date
        for customer_id, offer_date in earliest_offer.items()
        if customer_id not in closed_customer_ids
    }


def sweep_offer_wiedervorlagen() -> list[Task]:
    tenant_id = get_current_tenant_id()
    today = date.today()
    created: list[Task] = []

    for customer_id, offer_date in _earliest_open_offer_dates().items():
        days_since = (today - offer_date).days
        if days_since < 7:
            continue

        target_reason = (
            WiedervorlageReason.OFFER_OLDER_THAN_14_DAYS
            if days_since >= 14
            else WiedervorlageReason.OFFER_OLDER_THAN_7_DAYS
        )
        target_priority = Priority.HIGH if target_reason == WiedervorlageReason.OFFER_OLDER_THAN_14_DAYS else Priority.MEDIUM

        existing_open = Task.query.filter(
            Task.customer_id == customer_id,
            Task.status == TaskStatus.OPEN,
            Task.wiedervorlage_reason.in_(OFFER_AGE_REASONS),
        ).first()

        if existing_open is not None:
            if existing_open.wiedervorlage_reason != target_reason:
                existing_open.wiedervorlage_reason = target_reason
                existing_open.priority = target_priority
                existing_open.title = WIEDERVORLAGE_LABELS[target_reason]
                existing_open.due_date = today
            continue

        customer = Customer.query.filter_by(id=customer_id).first()
        if customer is None:
            continue

        task = Task(
            tenant_id=tenant_id,
            customer=customer,
            assigned_user_id=customer.assigned_user_id,
            type=TaskType.FOLLOW_UP_OFFER,
            wiedervorlage_reason=target_reason,
            title=WIEDERVORLAGE_LABELS[target_reason],
            priority=target_priority,
            status=TaskStatus.OPEN,
            due_date=today,
        )
        db.session.add(task)
        log_timeline_event(customer, TimelineEventType.TASK_CREATED, f"Aufgabe erstellt: {task.title}", task=task)
        created.append(task)

    if created:
        db.session.commit()
    return created
