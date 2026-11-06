"""Gemeinsamer Buerokalender: Rechte, Bueotrennung, Zeitumstellung, Konflikte, Nebenlaeufigkeit."""

from datetime import datetime

import pytest

from app.models import CalendarEvent, CalendarEventChange, Customer, Tenant, User, UserRole
from app.tenancy import bypass_tenant_scope, use_tenant_id

API = "/kalender/api"


def _login(app, email, password):
    client = app.test_client()
    client.post("/auth/login", data={"login_type": "email", "identifier": email, "password": password})
    return client


@pytest.fixture()
def laura(app, auth_client, employee):
    return _login(app, "mitarbeiter@example.com", "mitarbeiterpass123")


def _create(client, assignee_id, **extra):
    body = {"title": "Beratung", "start": "2026-11-12T14:00", "end": "2026-11-12T14:30", "assignee_id": assignee_id}
    body.update(extra)
    return client.post(f"{API}/termine", json=body)


def test_page_and_navigation_for_office_members(auth_client, laura):
    for client in (auth_client, laura):
        html = client.get("/kalender").get_data(as_text=True)
        assert 'id="cal-data"' in html
        assert 'href="/kalender" class="zs-item is-active"' in html


def test_create_edit_and_history_shared_in_office(auth_client, laura, user, employee):
    resp = _create(laura, user.id, customer_id=None, location_kind="telefon", note="Rückruf zur Kfz")
    assert resp.status_code == 201
    event = resp.get_json()["event"]
    assert event["start"] == "2026-11-12T14:00" and event["created_by"] and event["location_label"] == "Telefon"
    # Admin sieht und bearbeitet den Termin der Kollegin.
    listed = auth_client.get(f"{API}/termine?von=2026-11-09&bis=2026-11-15").get_json()["events"]
    assert [e["id"] for e in listed] == [event["id"]]
    patch = auth_client.patch(f"{API}/termine/{event['id']}", json={**event, "assignee_id": user.id, "title": "Beratung Kfz", "version": event["version"]})
    assert patch.status_code == 200 and patch.get_json()["event"]["version"] == 2
    history = laura.get(f"{API}/termine/{event['id']}").get_json()["history"]
    assert [h["action"] for h in history] == ["geändert", "angelegt"]
    assert "title" in history[0]["fields"]


def test_double_submit_creates_one_event(laura, employee):
    first = _create(laura, employee.id, client_ref="abc")
    second = _create(laura, employee.id, client_ref="abc")
    assert first.status_code == 201 and second.status_code == 200
    assert second.get_json()["created"] is False
    assert CalendarEvent.query.count() == 1


def test_overlap_is_warning_not_error(laura, employee):
    _create(laura, employee.id)
    resp = _create(laura, employee.id, title="Zweiter", start="2026-11-12T14:15", end="2026-11-12T15:00")
    assert resp.status_code == 201
    assert [c["title"] for c in resp.get_json()["conflicts"]] == ["Beratung"]
    check = laura.get(f"{API}/konflikte?start=2026-11-12T14:20&ende=2026-11-12T14:40&mitarbeiter={employee.id}").get_json()
    assert len(check["conflicts"]) == 2


def test_concurrent_edit_is_rejected(auth_client, laura, employee):
    event = _create(laura, employee.id).get_json()["event"]
    ok = laura.patch(f"{API}/termine/{event['id']}", json={**event, "title": "Laura", "version": 1})
    assert ok.status_code == 200
    stale = auth_client.patch(f"{API}/termine/{event['id']}", json={**event, "title": "Admin", "version": 1})
    assert stale.status_code == 409 and stale.get_json()["event"]["title"] == "Laura"
    assert CalendarEvent.query.one().title == "Laura"


def test_dst_storage_and_rejection(laura, employee):
    # Sommerzeit-Beginn 29.03.2026: 02:30 gibt es nicht.
    bad = _create(laura, employee.id, start="2026-03-29T02:30", end="2026-03-29T03:30")
    assert bad.status_code == 400 and "Zeitumstellung" in bad.get_json()["error"]
    # 01:30-03:30 am Umstellungstag sind real eine Stunde, Anzeige bleibt Wanduhrzeit.
    event = _create(laura, employee.id, start="2026-03-29T01:30", end="2026-03-29T03:30").get_json()["event"]
    stored = CalendarEvent.query.get(event["id"])
    assert (stored.ends_at - stored.starts_at).total_seconds() == 3600
    assert stored.starts_at == datetime(2026, 3, 29, 0, 30)  # UTC
    assert event["start"] == "2026-03-29T01:30" and event["end"] == "2026-03-29T03:30"
    # Winterzeit 25.10.2026: Termin ueber die Umstellung wird im Tageszeitraum gefunden.
    _create(laura, employee.id, title="Herbst", start="2026-10-25T01:00", end="2026-10-25T04:00")
    titles = [e["title"] for e in laura.get(f"{API}/termine?von=2026-10-25&bis=2026-10-25").get_json()["events"]]
    assert titles == ["Herbst"]


def test_validation_errors(laura, employee, user):
    assert _create(laura, employee.id, title=" ").status_code == 400
    assert _create(laura, employee.id, end="2026-11-12T13:00").status_code == 400
    assert _create(laura, 999999).status_code == 400


def test_cancel_reactivate_and_delete_rights(auth_client, laura, user, employee):
    event = _create(auth_client, employee.id).get_json()["event"]
    cancelled = laura.post(f"{API}/termine/{event['id']}/absagen", json={"version": event["version"]})
    assert cancelled.get_json()["event"]["status"] == "cancelled"
    v = cancelled.get_json()["event"]["version"]
    assert laura.post(f"{API}/termine/{event['id']}/reaktivieren", json={"version": v}).get_json()["event"]["status"] == "scheduled"
    # Laura hat den Termin nicht angelegt und ist keine Admin -> darf nicht loeschen.
    current = CalendarEvent.query.get(event["id"])
    assert laura.delete(f"{API}/termine/{event['id']}", json={"version": current.version}).status_code == 403
    assert auth_client.delete(f"{API}/termine/{event['id']}", json={"version": current.version}).status_code == 200
    assert CalendarEvent.query.count() == 0
    # Verlauf bleibt nach dem Loeschen erhalten.
    assert [c.action for c in CalendarEventChange.query.order_by(CalendarEventChange.id).all()] == ["create", "cancel", "reactivate", "delete"]


def test_other_office_is_isolated(app, db, laura, employee):
    event = _create(laura, employee.id).get_json()["event"]
    other = Tenant(name="Fremdbüro", slug="fremd-kal")
    db.session.add(other)
    db.session.commit()
    with use_tenant_id(other.id):
        stranger = User(tenant_id=other.id, email="fremd-kal@example.com", is_active=True, role=UserRole.OFFICE_ADMIN)
        stranger.set_password("fremdpasswort123")
        foreign_customer = Customer(tenant_id=other.id, name="Fremdkunde")
        db.session.add_all([stranger, foreign_customer])
        db.session.commit()
        foreign_customer_id = foreign_customer.id
    bob = _login(app, "fremd-kal@example.com", "fremdpasswort123")
    assert bob.get(f"{API}/termine?von=2026-11-09&bis=2026-11-15").get_json()["events"] == []
    assert bob.get(f"{API}/termine/{event['id']}").status_code == 404
    assert bob.patch(f"{API}/termine/{event['id']}", json={**event}).status_code == 404
    assert bob.delete(f"{API}/termine/{event['id']}", json={"version": 1}).status_code == 404
    # Fremde Mitarbeiter/Kunden lassen sich im eigenen Buero nicht zuordnen.
    assert _create(bob, employee.id).status_code == 400
    assert _create(laura, employee.id, customer_id=foreign_customer_id).status_code == 404
    with bypass_tenant_scope():
        assert CalendarEvent.query.count() == 1


def test_super_admin_has_no_access(app, db, tenant):
    with bypass_tenant_scope():
        admin = User(tenant_id=tenant.id, email="platform@example.com", is_active=True, role=UserRole.SUPER_ADMIN)
        admin.set_password("plattformpass123")
        db.session.add(admin)
        db.session.commit()
    client = _login(app, "platform@example.com", "plattformpass123")
    assert client.get("/kalender").status_code in (302, 403)
    assert client.get(f"{API}/termine").status_code in (302, 403)


def test_customer_search_and_stamp(laura, employee, db, tenant):
    db.session.add(Customer(tenant_id=tenant.id, name="Erika Kalendertest"))
    db.session.commit()
    names = [c["name"] for c in laura.get(f"{API}/kunden?q=Kalendertest").get_json()["customers"]]
    assert names == ["Erika Kalendertest"]
    before = laura.get(f"{API}/stand").get_json()["stamp"]
    _create(laura, employee.id)
    assert laura.get(f"{API}/stand").get_json()["stamp"] != before
