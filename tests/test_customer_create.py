# ruff: noqa: F811  (Fixtures werden aus anderen Testmodulen importiert)
"""Ausdrueckliche Kundenanlage im ZentriqAI-Feld - lokal, ohne KI-Anbieter (router.can_answer
ist in diesen Tests nicht aktiviert). Alle Pruefungen ueber echte HTTP-Requests."""

import pytest

from app.models import Customer, CustomerContact, CustomerNote
from app.services import customer_sources
from app.services.customer_create import parse
from app.tenancy import bypass_tenant_scope
from tests.test_customer_notes import API, _input, office  # noqa: F401
from tests.test_permissions_security import login, world  # noqa: F401

EXAMPLE = "lege kunden justin heller an privat 01795223152 wilhelmstrasse 7 53809"


def _by_name(tenant_id, name):
    with bypass_tenant_scope():
        return Customer.query.filter_by(tenant_id=tenant_id, name=name).order_by(Customer.id).all()


def _no_notes_or_callbacks():
    with bypass_tenant_scope():
        return CustomerNote.query.count() == 0 and CustomerContact.query.count() == 0


@pytest.fixture()
def fresh_office(app, db, world):
    """Buero ohne gleichnamigen Bestand (das office-Fixture enthaelt bereits einen Justin Heller)."""
    return world


# --- Zerlegung (lokale Regeln) -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (EXAMPLE, ("Justin", "Heller", "+491795223152", "Wilhelmstrasse", "7", "53809", None)),
        ("LEGE KUNDEN JUSTIN HELLER AN 0179 5223152 WILHELMSTRASSE 7 53809", ("Justin", "Heller", "+491795223152", "Wilhelmstrasse", "7", "53809", None)),
        ("Neuer Kunde: Anna-Lena Müller-Lüdenscheidt, Tel. 0228 / 123 456, Am Markt 3a, 53111 Bonn",
         ("Anna-Lena", "Müller-Lüdenscheidt", "+49228123456", "Am Markt", "3a", "53111", "Bonn")),
        ("Kunden anlegen Heller, Justin +49 179 5223152 53809 Ruppichteroth", ("Justin", "Heller", "+491795223152", None, None, "53809", "Ruppichteroth")),
        ("Lege Maria von der Heide als Kunden an, Bonner Straße 12, 53227 Bonn", ("Maria", "von der Heide", None, "Bonner Straße", "12", "53227", "Bonn")),
        ("neue kundin jürgen o'brien an der kirche 5", ("Jürgen", "O'Brien", None, "An der Kirche", "5", None, None)),
    ],
)
def test_parse_fields(text, expected):
    d = parse(text)
    phone = d.phones[0] if d.phones else None
    assert (d.first_name, d.last_name, phone, d.street, d.house_number, d.postal_code, d.city) == expected
    assert d.vn is None or "808" in text


# --- Beispiel aus dem Auftrag ------------------------------------------------------------------


def test_example_creates_customer_with_structured_fields(app, fresh_office):
    laura = login(app, "laura@example.com")
    data = _input(laura, EXAMPLE, client_ref="c1")
    assert data["kind"] == "card" and data["created"] is True
    assert data["message"] == "Justin Heller wurde angelegt."
    [customer] = _by_name(fresh_office.tenant_a.id, "Justin Heller")
    assert (customer.first_name, customer.last_name, customer.phone_private, customer.street, customer.house_number, customer.postal_code) == (
        "Justin", "Heller", "0179 5223152", "Wilhelmstrasse", "7", "53809")
    assert customer.phone is None and customer.phone_landline is None
    assert customer.address == "Wilhelmstrasse 7"
    # Nicht genannte Angaben bleiben leer - keine VN-Nummer noetig, kein Ort erfunden.
    assert (customer.customer_number, customer.city, customer.email, customer.date_of_birth) == (None, None, None, None)
    assert customer.phone_private_key == "+491795223152" and customer.source == customer_sources.EINGABE
    assert customer.assigned_user_id == fresh_office.laura_id
    assert _no_notes_or_callbacks()
    card = data["card"]
    assert card["id"] == customer.id and card["can_edit"] is True and card["edit"]["values"]["street"] == "Wilhelmstrasse"


def test_found_by_name_and_phone_for_colleagues_and_not_other_office(app, fresh_office):
    _input(login(app, "laura@example.com"), EXAMPLE)
    admin = login(app, "admin-a@example.com")
    by_name = _input(admin, "Justin Heller")
    assert by_name["kind"] == "card" and by_name["card"]["phone"] == "privat 0179 5223152"
    by_phone = _input(admin, "0179 522 31 52")
    assert by_phone["kind"] == "card" and by_phone["card"]["name"] == "Justin Heller"
    # Fremdes Buero sieht den Kunden nicht.
    bob = login(app, "bob@example.com")
    resp = bob.post(f"{API}/kunden/eingabe", json={"text": "0179 5223152"}).get_json()
    assert resp.get("kind") != "card"


def test_search_alone_never_creates(app, fresh_office):
    laura = login(app, "laura@example.com")
    for text in ("Justin Heller", "01795223152", "justin heller 01795223152 wilhelmstrasse 7"):
        data = _input(laura, text)
        assert not data.get("created")
    assert _by_name(fresh_office.tenant_a.id, "Justin Heller") == []


def test_repeat_and_double_submit_create_no_duplicate(app, fresh_office):
    laura = login(app, "laura@example.com")
    first = _input(laura, EXAMPLE, client_ref="a")
    second = _input(laura, EXAMPLE, client_ref="a")
    third = _input(login(app, "admin-a@example.com"), EXAMPLE, client_ref="b")
    assert first["created"] is True and not second.get("created") and not third.get("created")
    assert second["notice"] == "Justin Heller ist bereits angelegt."
    assert len(_by_name(fresh_office.tenant_a.id, "Justin Heller")) == 1


def test_existing_customer_is_shown_not_overwritten(app, office):
    """Bestand: Justin Heller mit 0171 1234567. Gleicher Name, gleiche Nummer -> vorhandene Karte;
    zusaetzliche Adresse wird nicht ungefragt eingetragen."""
    laura = login(app, "laura@example.com")
    data = _input(laura, "neuer kunde justin heller privat 0171 1234567 wilhelmstrasse 7 53809")
    assert data["kind"] == "card" and data["card"]["id"] == office.ids["Justin Heller"] and data.get("existing")
    assert "Nicht übernommen (Adresse, PLZ)" in data["notice"]
    with bypass_tenant_scope():
        customer = Customer.query.get(office.ids["Justin Heller"])
        assert customer.address is None and customer.postal_code is None


def test_same_name_with_other_phone_asks(app, office):
    laura = login(app, "laura@example.com")
    data = _input(laura, EXAMPLE)
    assert data["kind"] == "choose" and data["create_option"] is True
    with bypass_tenant_scope():
        assert Customer.query.filter_by(tenant_id=office.tenant_a.id).count() == office.customer_count  # nichts angelegt
    created = _input(laura, EXAMPLE, mode="create", create_new=True, first_name="Justin", last_name="Heller")
    assert created["created"] is True and len(_by_name(office.tenant_a.id, "Justin Heller")) == 2


def test_phone_of_another_person_is_no_merge_reason(app, office):
    laura = login(app, "laura@example.com")
    data = _input(laura, "lege kunden sabine heller an festnetz 0171 1234567")
    assert data["created"] is True and data["card"]["name"] == "Sabine Heller"
    assert "auch bei Justin Heller hinterlegt" in data["notice"]
    with bypass_tenant_scope():
        assert Customer.query.get(office.ids["Justin Heller"]).name == "Justin Heller"


def test_without_vn_and_with_incomplete_address(app, fresh_office):
    laura = login(app, "laura@example.com")
    data = _input(laura, "Neuer Kunde Erika Mustermann Lindenstraße")
    assert data["created"] is True
    [c] = _by_name(fresh_office.tenant_a.id, "Erika Mustermann")
    assert (c.street, c.house_number, c.postal_code, c.phone, c.customer_number) == ("Lindenstraße", None, None, None, None)


def test_multi_part_name_asks_and_keeps_recognized_data(app, fresh_office):
    laura = login(app, "laura@example.com")
    text = "lege kunden hans peter schulz an 0171 9998887 53809"
    data = _input(laura, text)
    assert data["kind"] == "create_name"
    assert [o["first_name"] for o in data["options"]] == ["Hans Peter", "Hans"]
    assert "Telefon 0171 9998887 (Typ offen)" in data["create"]["facts"]
    asked = _input(laura, data["create"]["text"], mode="create", first_name="Hans Peter", last_name="Schulz")
    assert asked["kind"] == "create_phone" and _by_name(fresh_office.tenant_a.id, "Hans Peter Schulz") == []  # Typ nicht geraten
    done = _input(laura, data["create"]["text"], mode="create", first_name="Hans Peter", last_name="Schulz", phone_kinds=["festnetz"])
    assert done["created"] is True
    [c] = _by_name(fresh_office.tenant_a.id, "Hans Peter Schulz")
    assert (c.first_name, c.last_name, c.phone_landline, c.phone_private, c.postal_code) == ("Hans Peter", "Schulz", "0171 9998887", None, "53809")


def test_single_word_name_asks(app, fresh_office):
    data = _input(login(app, "laura@example.com"), "neuer kunde heller 0171 9998887")
    assert data["kind"] == "create_name" and data["suggestion"]["last_name"] == "Heller"


def test_particle_name(app, fresh_office):
    data = _input(login(app, "laura@example.com"), "Lege Maria von der Heide als Kunden an")
    assert data["created"] is True
    [c] = _by_name(fresh_office.tenant_a.id, "Maria von der Heide")
    assert (c.first_name, c.last_name) == ("Maria", "von der Heide")


# --- Bearbeiten ueber die Karte ----------------------------------------------------------------


def test_edit_card_fields_with_permissions_and_version(app, fresh_office):
    laura = login(app, "laura@example.com")
    card = _input(laura, EXAMPLE)["card"]
    body = {**card["edit"]["values"], "city": "Ruppichteroth", "house_number": "7a", "version": card["edit"]["version"]}
    for key in ("name", "address"):
        body.pop(key)
    resp = laura.patch(f"{API}/kunden/{card['id']}", json=body)
    assert resp.status_code == 200, resp.get_json()
    with bypass_tenant_scope():
        c = Customer.query.get(card["id"])
        assert (c.city, c.house_number, c.address) == ("Ruppichteroth", "7a", "Wilhelmstrasse 7a")
        assert c.field_sources["city"] == customer_sources.MANUELL
    # Veraltete Version -> 409, nichts ueberschrieben.
    assert laura.patch(f"{API}/kunden/{card['id']}", json={**body, "city": "Bonn"}).status_code == 409
    # Ungueltige Telefonnummer
    fresh = laura.get(f"{API}/kunden/{card['id']}").get_json()["card"]["edit"]["version"]
    assert laura.patch(f"{API}/kunden/{card['id']}", json={**body, "phone": "12", "version": fresh}).status_code == 400
    # Fremdes Buero: 404
    bob = login(app, "bob@example.com")
    assert bob.patch(f"{API}/kunden/{card['id']}", json=body).status_code == 404


def test_employee_cannot_edit_customers_of_others(app, office):
    laura = login(app, "laura@example.com")
    card = laura.get(f"{API}/kunden/{office.ids['Maria Schulz']}").get_json()["card"]
    assert card["can_edit"] is False
    assert laura.patch(f"{API}/kunden/{office.ids['Maria Schulz']}", json={"name": "X", "version": ""}).status_code == 403


# --- Festnetz und Privattelefon ----------------------------------------------------------------


def test_both_numbers_by_keyword_and_found_by_each(app, fresh_office):
    laura = login(app, "laura@example.com")
    data = _input(laura, "Neuer Kunde Erika Probe, Festnetz 02641 123456, privat 0171 2223334")
    assert data["created"] is True
    [c] = _by_name(fresh_office.tenant_a.id, "Erika Probe")
    assert (c.phone_landline, c.phone_private, c.phone) == ("02641123456", "0171 2223334", None)
    assert data["card"]["phone"] == "Festnetz 02641123456 · privat 0171 2223334"
    for number in ("02641/123456", "+49 2641 123456", "0171 222 33 34"):
        found = _input(laura, number)
        assert found["kind"] == "card" and found["card"]["id"] == c.id, number
    html = login(app, "admin-a@example.com").get("/search?q=0171+2223334").get_data(as_text=True)
    assert "Erika Probe" in html
    assert "Erika Probe" in login(app, "admin-a@example.com").get("/search?q=02641123456").get_data(as_text=True)


def test_single_number_without_keyword_asks_never_guesses(app, fresh_office):
    laura = login(app, "laura@example.com")
    data = _input(laura, "Neuer Kunde Paul Probe 0171 5556667")
    assert data["kind"] == "create_phone" and data["message"] == "Ist 0171 5556667 Festnetz oder Privattelefon?"
    assert [o["kinds"] for o in data["options"]] == [["festnetz"], ["privat"]]
    assert _by_name(fresh_office.tenant_a.id, "Paul Probe") == []
    done = _input(laura, data["create"]["text"], mode="create", phone_kinds=["privat"])
    [c] = _by_name(fresh_office.tenant_a.id, "Paul Probe")
    assert done["created"] is True and (c.phone_private, c.phone_landline) == ("0171 5556667", None)
    # Eine benannte und eine unbenannte Nummer: nur die unbenannte wird erfragt
    data = _input(laura, "Neuer Kunde Rita Probe festnetz 02641 98765 0172 1112223")
    assert data["kind"] == "create_phone" and [o["kinds"] for o in data["options"]] == [["privat"]]
    # Zwei unbenannte Nummern: Zuordnung waehlen
    data = _input(laura, "Neuer Kunde Uwe Probe 02641 55555 0173 4445556")
    assert data["kind"] == "create_phone" and len(data["options"]) == 2
    done = _input(laura, data["create"]["text"], mode="create", phone_kinds=data["options"][1]["kinds"])
    [u] = _by_name(fresh_office.tenant_a.id, "Uwe Probe")
    assert (u.phone_landline, u.phone_private) == ("0173 4445556", "0264155555")


def test_existing_untyped_phone_kept_shown_and_found(app, office):
    """Bestand (office-Fixture): Justin Heller mit bisheriger Nummer 0171 1234567 ohne Typ."""
    laura = login(app, "laura@example.com")
    with bypass_tenant_scope():
        c = Customer.query.get(office.ids["Justin Heller"])
        assert c.phone and not c.phone_landline and not c.phone_private
    found = _input(laura, "0171 1234567")
    assert found["kind"] == "card" and found["card"]["id"] == office.ids["Justin Heller"] and found["card"]["phone"] == c.phone
    admin = login(app, "admin-a@example.com")
    card = admin.get(f"{API}/kunden/{c.id}").get_json()["card"]
    assert card["edit"]["legacy_phone"] is True and card["edit"]["values"]["phone"] == c.phone
    # Festnetz ergaenzen, bisherige Nummer bleibt unveraendert
    body = {"phone_landline": "02641 77777", "version": card["edit"]["version"]}
    assert admin.patch(f"{API}/kunden/{c.id}", json=body).status_code == 200
    with bypass_tenant_scope():
        c = Customer.query.get(office.ids["Justin Heller"])
        assert (c.phone_landline, c.phone_key) == ("0264177777", "+491711234567")
    assert _input(laura, "02641 77777")["card"]["id"] == c.id and _input(laura, "0171 1234567")["card"]["id"] == c.id
