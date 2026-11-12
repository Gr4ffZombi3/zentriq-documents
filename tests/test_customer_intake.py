# ruff: noqa: F811  (Fixture "world" wird aus test_permissions_security importiert)
"""Kunde anlegen mit "Kundendaten einfuegen" (Kundenverwaltung > Kunde anlegen). Fiktive Daten."""

import pytest

from app.models import Customer
from app.services.customer_intake import parse_block
from app.tenancy import bypass_tenant_scope, use_tenant_id
from tests.test_permissions_security import login, world  # noqa: F401

SIGNATURE = """Hallo Frau Andres,

anbei wie besprochen meine Daten für das Angebot.

Mit freundlichen Grüßen
Max Mustermann
Musterstraße 12
53111 Bonn
Tel.: 0228 1234567
Mobil: 0171 7654321
max.mustermann@example.com"""


def test_email_section_with_signature():
    f = parse_block(SIGNATURE)
    assert (f["first_name"], f["last_name"]) == ("Max", "Mustermann")
    assert (f["street"], f["house_number"], f["postal_code"], f["city"]) == ("Musterstraße", "12", "53111", "Bonn")
    assert f["email"] == "max.mustermann@example.com"
    assert f["phone_private"].replace(" ", "") == "01717654321" and f["phone_landline"].replace(" ", "") == "02281234567"


@pytest.mark.parametrize("text,expected", [
    ("Erika Beispiel\nAm Hang 5a\n50667 Köln", {"first_name": "Erika", "last_name": "Beispiel", "street": "Am Hang", "house_number": "5a"}),
    ("53111 Bonn, Hauptstr. 3, Peter Krause, peter@example.de", {"first_name": "Peter", "last_name": "Krause", "city": "Bonn"}),
    ("Lindenallee 7\n53119 Bonn\nHerrn Dr. Jonas von Berg", {"first_name": "Jonas", "last_name": "von Berg", "street": "Lindenallee"}),
    ("Anna Weinberg\nRosenweg 3\n53113 Bonn", {"last_name": "Weinberg", "street": "Rosenweg"}),
])
def test_different_orders_and_layouts(text, expected):
    found = parse_block(text)
    for key, value in expected.items():
        assert found.get(key) == value, (text, found)


def test_street_is_never_taken_as_name_and_unclear_stays_empty():
    found = parse_block("Bahnhofstraße 1\n53111 Bonn")
    assert "first_name" not in found and "last_name" not in found and found["street"] == "Bahnhofstraße"
    # zwei gleichwertige Namen ohne Adressbezug: unklar -> leer
    assert "last_name" not in parse_block("Max Mustermann\nErika Beispiel")


def test_detect_saves_nothing_and_page_has_paste_field(app, world):
    client = login(app, "laura@example.com")
    html = client.get("/customers/neu").get_data(as_text=True)
    assert "Kundendaten einfügen" in html and 'name="first_name"' in html
    with bypass_tenant_scope():
        before = Customer.query.count()
    data = client.post("/customers/neu/erkennen", json={"text": SIGNATURE}).get_json()
    assert data["fields"]["last_name"] == "Mustermann"
    with bypass_tenant_scope():
        assert Customer.query.count() == before
    assert "Kunde anlegen" in client.get("/customers").get_data(as_text=True)


def test_one_click_save_without_duplicate(app, world):
    client = login(app, "laura@example.com")
    resp = client.post("/customers/neu", data={**parse_block(SIGNATURE), "first_name": "Max", "last_name": "Mustermann"})
    assert resp.status_code == 302 and "/customers/" in resp.headers["Location"]
    with use_tenant_id(world.tenant_a.id):
        customer = Customer.query.filter_by(name="Max Mustermann").one()
        assert customer.city == "Bonn" and customer.email == "max.mustermann@example.com"
        assert customer.field_sources["phone_private"] == "MANUELL"


def test_duplicate_shows_open_file_and_needs_confirmation(app, db, world):
    with use_tenant_id(world.tenant_a.id):
        existing = Customer(tenant_id=world.tenant_a.id, name="Erika Beispiel", phone_private="0171 5555555")
        db.session.add(existing)
        db.session.commit()
        existing_id = existing.id
    client = login(app, "laura@example.com")
    form = {"first_name": "Erika", "last_name": "Beispiel", "city": "Köln"}
    check = client.post("/customers/neu/pruefen", json=form).get_json()
    assert check["duplicates"][0]["id"] == existing_id and check["duplicates"][0]["url"] == f"/customers/{existing_id}"
    resp = client.post("/customers/neu", data=form)
    html = resp.get_data(as_text=True)
    assert resp.status_code == 409 and "Akte öffnen" in html and f"/customers/{existing_id}" in html
    with use_tenant_id(world.tenant_a.id):
        assert Customer.query.filter_by(name="Erika Beispiel").count() == 1
    assert client.post("/customers/neu", data={**form, "trotzdem": "1"}).status_code == 302
    with use_tenant_id(world.tenant_a.id):
        assert Customer.query.filter_by(name="Erika Beispiel").count() == 2
    # Telefonnummer allein ist ebenfalls ein konkreter Verdacht
    phone = client.post("/customers/neu/pruefen", json={"first_name": "Anna", "last_name": "Anders", "phone_private": "0171 5555555"}).get_json()
    assert phone["duplicates"] and phone["duplicates"][0]["reason"] == "gleiche Telefonnummer"


def test_validation_and_office_isolation(app, db, world):
    with use_tenant_id(world.tenant_b.id):
        db.session.add(Customer(tenant_id=world.tenant_b.id, name="Fremd Kunde"))
        db.session.commit()
    client = login(app, "dennis@example.com")  # Mitarbeiter
    assert client.post("/customers/neu", data={"first_name": "Nur"}).status_code == 422
    assert client.post("/customers/neu", data={"first_name": "Ole", "last_name": "Neu", "postal_code": "123"}).status_code == 422
    assert client.post("/customers/neu/pruefen", json={"first_name": "Fremd", "last_name": "Kunde"}).get_json()["duplicates"] == []
    assert client.post("/customers/neu", data={"first_name": "Fremd", "last_name": "Kunde"}).status_code == 302
    assert login(app, "justin@example.com").get("/customers/neu").status_code == 403
