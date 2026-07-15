"""Baut Eintraege fuer die Kunden-Chronologie (CustomerTimelineEvent). `label` kommt
immer aus einem festen, serverseitig gebauten Text - nie aus freiem Nutzertext -, damit
die Chronologie konsistent bleibt."""

from datetime import datetime, timezone

from app.extensions import db
from app.models import Customer, CustomerTimelineEvent, Document, Task
from app.models.enums import TimelineEventType


def log_timeline_event(
    customer: Customer,
    event_type: TimelineEventType,
    label: str,
    *,
    document: Document | None = None,
    task: Task | None = None,
    occurred_at: datetime | None = None,
    extra_data: dict | None = None,
) -> CustomerTimelineEvent:
    event = CustomerTimelineEvent(
        tenant_id=customer.tenant_id,
        customer=customer,
        document=document,
        task=task,
        event_type=event_type,
        label=label,
        occurred_at=occurred_at or datetime.now(timezone.utc),
        extra_data=extra_data,
    )
    db.session.add(event)
    return event
