"""Dubletten-Erkennung (Kundennummer, Telefon, Name) und Zusammenfuehren ohne Datenverlust -
ausschliesslich innerhalb des eigenen Bueros."""

from datetime import date

import pytest

from app.models import (
    AuditLog,
    Customer,
    CustomerTimelineEvent,
    DocStatus,
    DocType,
    Document,
    DocumentCustomer,
    Task,
    Tenant,
    UserRole,
)
from app.models.audit_log import AuditEventType
from app.models.enums import TaskStatus, TaskType, TimelineEventType
from app.services.customer_duplicates import duplicate_reason, find_duplicates
from app.tenancy import bypass_tenant_scope, use_tenant_id
from tests.test_global_search import _login, _make_user


def _customer(db, tenant_id, name, **fields):
    with use_tenant_id(tenant_id):
        customer = Customer(tenant_id=tenant_id, name=name, **fields)
        db.session.add(customer)
        db.session.commit()
        return customer


@pytest.fixture()
def office(app, db, tenant):
    _make_user(db, tenant.id, "admin@example.org", UserRole.OFFICE_ADMIN, "08/0001-A")
    _make_user(db, tenant.id, "ma@example.org", UserRole.EMPLOYEE, "08/0002-A")
    _make_user(db, tenant.id, "plattform@example.org", UserRole.SUPER_ADMIN, None)
    return tenant


def test_keys_are_normalized_and_kept_up_to_date(db, tenant):
    customer = _customer(db, tenant.id, "Mustermann, Max", phone="+49 (0)171 123 4567", customer_number="k-123 456")
    assert (customer.name_key, customer.phone_key, customer.customer_number_key) == ("max mustermann", "+491711234567", "K123456")
    customer.phone = "0049 171 7654321"
    db.session.commit()
    assert customer.phone_key == "+491717654321"


@pytest.mark.parametrize(
    "first, second, reason",
    [
        ({"name": "Anna A", "customer_number": "K-1"}, {"name": "Berta B", "customer_number": "k 1"}, "customer_number"),
        ({"name": "Anna A", "phone": "0171 1234567"}, {"name": "Anna Andere", "phone": "+49 171 1234567"}, "phone"),
        ({"name": "Mustermann, Max"}, {"name": "Max Mustermann"}, "name"),
    ],
)
def test_duplicates_are_found_by_number_phone_and_name(db, tenant, first, second, reason):
    a = _customer(db, tenant.id, **first)
    b = _customer(db, tenant.id, **second)
    assert duplicate_reason(a, b) == reason
    assert [item.customer.id for item in find_duplicates(a)] == [b.id]


def test_clearly_different_people_are_not_duplicates(db, tenant):
    a = _customer(db, tenant.id, "Max Mustermann", phone="0171 1234567", customer_number="K-1")
    b = _customer(db, tenant.id, "Max Mustermann", phone="0171 1234567", customer_number="K-2")
    c = _customer(db, tenant.id, "Erika Muster", date_of_birth=date(1980, 1, 1))
    d = _customer(db, tenant.id, "Erika Muster", date_of_birth=date(1990, 1, 1))
    assert duplicate_reason(a, b) is None
    assert duplicate_reason(c, d) is None


def test_duplicates_are_never_searched_across_offices(app, db, office):
    other = Tenant(name="Fremdes Büro", slug="fremd")
    db.session.add(other)
    db.session.commit()
    own = _customer(db, office.id, "Max Mustermann", phone="0171 1234567", customer_number="K-1")
    foreign = _customer(db, other.id, "Max Mustermann", phone="0171 1234567", customer_number="K-1")
    with use_tenant_id(office.id):
        assert find_duplicates(own) == []
    client = _login(app, "admin@example.org")
    assert client.get(f"/customers/{own.id}/dublette/{foreign.id}").status_code == 404
    assert client.post(f"/customers/{own.id}/zusammenfuehren/{foreign.id}", data={"bestaetigt": "ja"}).status_code == 404
    with bypass_tenant_scope():
        assert Customer.query.filter_by(id=foreign.id).count() == 1


def test_list_and_detail_show_hint_and_compare_link(app, db, office):
    a = _customer(db, office.id, "Max Mustermann", phone="0171 1234567")
    b = _customer(db, office.id, "Mustermann, Max")
    client = _login(app, "admin@example.org")
    assert "Mögliche Dublette" in client.get("/customers").get_data(as_text=True)
    detail = client.get(f"/customers/{a.id}").get_data(as_text=True)
    assert f"/customers/{a.id}/dublette/{b.id}" in detail
    compare = client.get(f"/customers/{a.id}/dublette/{b.id}").get_data(as_text=True)
    assert "Mögliche Dublette" in compare and "gleicher Name" in compare and "Zusammenführen" in compare


def _make_linked_data(db, tenant_id, target, source):
    with use_tenant_id(tenant_id):
        shared = Document(tenant_id=tenant_id, filename="a.pdf", original_filename="a.pdf", file_path="/tmp/a.pdf",
                          status=DocStatus.DONE, doc_type=DocType.LEIPZIGER_LISTE)
        only_source = Document(tenant_id=tenant_id, filename="b.pdf", original_filename="b.pdf", file_path="/tmp/b.pdf",
                               status=DocStatus.DONE, doc_type=DocType.LEIPZIGER_LISTE, customer_id=source.id)
        db.session.add_all([shared, only_source])
        db.session.flush()
        db.session.add_all([
            DocumentCustomer(tenant_id=tenant_id, document_id=shared.id, customer_id=target.id, row_data=[{"contract_number": "1"}]),
            DocumentCustomer(tenant_id=tenant_id, document_id=shared.id, customer_id=source.id, row_data=[{"contract_number": "2"}]),
            DocumentCustomer(tenant_id=tenant_id, document_id=only_source.id, customer_id=source.id, row_data=[{"contract_number": "3"}]),
            Task(tenant_id=tenant_id, customer_id=source.id, title="Rückruf", type=TaskType.CALL_TODAY, status=TaskStatus.OPEN),
            CustomerTimelineEvent(tenant_id=tenant_id, customer_id=source.id, event_type=TimelineEventType.DOCUMENT_UPLOADED,
                                  label="Dokument hochgeladen", occurred_at=date(2026, 9, 1)),
        ])
        db.session.commit()
        return shared.id, only_source.id


def test_merge_keeps_all_information_and_links(app, db, office):
    target = _customer(db, office.id, "Max Mustermann", phone="0171 1234567", city="Leipzig")
    source = _customer(db, office.id, "Mustermann, Max", phone="+49 171 1234567", customer_number="K-77",
                       email="max@example.org", city="Halle", postal_code="06108")
    target_id, source_id = target.id, source.id
    shared_id, only_source_id = _make_linked_data(db, office.id, target, source)

    client = _login(app, "admin@example.org")
    resp = client.post(f"/customers/{target_id}/zusammenfuehren/{source_id}", data={"bestaetigt": "ja"})
    assert resp.status_code == 302

    db.session.expire_all()
    with use_tenant_id(office.id):
        merged = db.session.get(Customer, target_id)
        assert db.session.get(Customer, source_id) is None
        # Leere Felder ergaenzt, gefuellte nicht ueberschrieben.
        assert (merged.customer_number, merged.email, merged.postal_code) == ("K-77", "max@example.org", "06108")
        assert (merged.name, merged.phone, merged.city) == ("Max Mustermann", "0171 1234567", "Leipzig")
        # Verknuepfungen: gemeinsame Liste zusammengelegt, alles andere umgehaengt.
        links = {link.document_id: link for link in DocumentCustomer.query.filter_by(customer_id=target_id)}
        assert [row["contract_number"] for row in links[shared_id].row_data] == ["1", "2"]
        assert only_source_id in links
        assert db.session.get(Document, only_source_id).customer_id == target_id
        assert Task.query.filter_by(customer_id=target_id).count() == 1
        events = CustomerTimelineEvent.query.filter_by(customer_id=target_id).all()
        assert any(e.event_type == TimelineEventType.DOCUMENT_UPLOADED for e in events)
        merge_event = next(e for e in events if e.event_type == TimelineEventType.CUSTOMER_MERGED)
        # Abweichender Ort bleibt im Verlauf erhalten; gleiche Telefonnummer/Name gelten nicht als abweichend.
        assert merge_event.extra_data["differing_values"] == {"Ort": "Halle"}
    audit = AuditLog.query.filter_by(event_type=AuditEventType.CUSTOMER_MERGED).one()
    assert audit.details["customer_id"] == target_id and audit.details["merged_customer_id"] == source_id


def test_merge_requires_confirmation_and_office_admin(app, db, office):
    a = _customer(db, office.id, "Max Mustermann")
    b = _customer(db, office.id, "Mustermann, Max")
    admin = _login(app, "admin@example.org")
    assert admin.post(f"/customers/{a.id}/zusammenfuehren/{b.id}").status_code == 302
    for email in ("ma@example.org", "plattform@example.org"):
        client = _login(app, email)
        assert client.get(f"/customers/{a.id}/dublette/{b.id}").status_code == 403
        assert client.post(f"/customers/{a.id}/zusammenfuehren/{b.id}", data={"bestaetigt": "ja"}).status_code == 403
    with use_tenant_id(office.id):
        assert Customer.query.count() == 2


def test_non_duplicates_cannot_be_merged(app, db, office):
    a = _customer(db, office.id, "Anna Anders")
    b = _customer(db, office.id, "Berta Beispiel")
    client = _login(app, "admin@example.org")
    assert client.post(f"/customers/{a.id}/zusammenfuehren/{b.id}", data={"bestaetigt": "ja"}).status_code == 404
