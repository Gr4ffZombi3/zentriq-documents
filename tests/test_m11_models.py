from datetime import datetime, timezone

import pytest

from app.models import (
    Customer,
    CustomerTimelineEvent,
    DocStatus,
    Document,
    ListChangeType,
    ListComparison,
    ListComparisonEntry,
    Priority,
    Task,
    TaskStatus,
    TaskType,
    Tenant,
    TimelineEventType,
)
from app.tenancy import MissingTenantContextError, set_current_tenant_id


def make_customer(db, tenant_id, name="Max Mustermann"):
    customer = Customer(tenant_id=tenant_id, name=name)
    db.session.add(customer)
    db.session.commit()
    return customer


def make_document(db, tenant_id, filename="a.pdf"):
    document = Document(
        tenant_id=tenant_id,
        filename=filename,
        original_filename=filename,
        file_path=f"/tmp/{filename}",
        status=DocStatus.DONE,
    )
    db.session.add(document)
    db.session.commit()
    return document


def make_task(db, tenant_id, customer_id=None, document_id=None, title="Heute anrufen"):
    task = Task(
        tenant_id=tenant_id,
        customer_id=customer_id,
        document_id=document_id,
        type=TaskType.CALL_TODAY,
        title=title,
        priority=Priority.HIGH,
        status=TaskStatus.OPEN,
    )
    db.session.add(task)
    db.session.commit()
    return task


def make_list_comparison(db, tenant_id, document_id):
    comparison = ListComparison(tenant_id=tenant_id, document_id=document_id)
    db.session.add(comparison)
    db.session.commit()
    return comparison


def make_timeline_event(db, tenant_id, customer_id):
    event = CustomerTimelineEvent(
        tenant_id=tenant_id,
        customer_id=customer_id,
        event_type=TimelineEventType.DOCUMENT_UPLOADED,
        label="Dokument hochgeladen",
        occurred_at=datetime.now(timezone.utc),
    )
    db.session.add(event)
    db.session.commit()
    return event


@pytest.mark.parametrize(
    "query_fn",
    [
        lambda: Task.query.all(),
        lambda: ListComparison.query.all(),
        lambda: ListComparisonEntry.query.all(),
        lambda: CustomerTimelineEvent.query.all(),
    ],
)
def test_new_m11_tables_require_tenant_context(db, query_fn):
    set_current_tenant_id(None)
    with pytest.raises(MissingTenantContextError):
        query_fn()


def test_tasks_are_isolated_between_tenants(app, db, tenant):
    tenant_b = Tenant(name="Tenant B", slug="tenant-b-m11-tasks")
    db.session.add(tenant_b)
    db.session.commit()

    customer_a = make_customer(db, tenant.id)
    task_a = make_task(db, tenant.id, customer_id=customer_a.id)

    set_current_tenant_id(tenant_b.id)
    customer_b = make_customer(db, tenant_b.id)
    task_b = make_task(db, tenant_b.id, customer_id=customer_b.id)

    set_current_tenant_id(tenant.id)
    assert Task.query.all() == [task_a]

    set_current_tenant_id(tenant_b.id)
    assert Task.query.all() == [task_b]


def test_customer_timeline_events_are_isolated_between_tenants(app, db, tenant):
    tenant_b = Tenant(name="Tenant B", slug="tenant-b-m11-timeline")
    db.session.add(tenant_b)
    db.session.commit()

    customer_a = make_customer(db, tenant.id)
    event_a = make_timeline_event(db, tenant.id, customer_a.id)

    set_current_tenant_id(tenant_b.id)
    customer_b = make_customer(db, tenant_b.id)
    event_b = make_timeline_event(db, tenant_b.id, customer_b.id)

    set_current_tenant_id(tenant.id)
    assert CustomerTimelineEvent.query.all() == [event_a]

    set_current_tenant_id(tenant_b.id)
    assert CustomerTimelineEvent.query.all() == [event_b]


def test_list_comparisons_are_isolated_between_tenants(app, db, tenant):
    tenant_b = Tenant(name="Tenant B", slug="tenant-b-m11-listcomp")
    db.session.add(tenant_b)
    db.session.commit()

    document_a = make_document(db, tenant.id, "list_a.pdf")
    comparison_a = make_list_comparison(db, tenant.id, document_a.id)

    set_current_tenant_id(tenant_b.id)
    document_b = make_document(db, tenant_b.id, "list_b.pdf")
    comparison_b = make_list_comparison(db, tenant_b.id, document_b.id)

    set_current_tenant_id(tenant.id)
    assert ListComparison.query.all() == [comparison_a]

    set_current_tenant_id(tenant_b.id)
    assert ListComparison.query.all() == [comparison_b]


def test_list_comparison_entry_relates_to_its_comparison(app, db, tenant):
    document = make_document(db, tenant.id, "list.pdf")
    customer = make_customer(db, tenant.id)
    comparison = make_list_comparison(db, tenant.id, document.id)

    entry = ListComparisonEntry(
        tenant_id=tenant.id,
        list_comparison_id=comparison.id,
        customer_id=customer.id,
        change_type=ListChangeType.NEW_CUSTOMER,
    )
    db.session.add(entry)
    db.session.commit()

    assert ListComparisonEntry.query.all() == [entry]
    assert comparison.entries == [entry]


def test_document_and_customer_relationships_to_new_tables(app, db, tenant):
    document = make_document(db, tenant.id)
    customer = make_customer(db, tenant.id)
    task = make_task(db, tenant.id, customer_id=customer.id, document_id=document.id)

    assert document.tasks == [task]
    assert customer.tasks == [task]
