# ruff: noqa: F811  (Fixtures werden aus anderen Testmodulen importiert)
"""Kundennotiz mit automatischer Neuanlage, wenn kein passender Kunde existiert."""

from datetime import date

from app.models import Customer, CustomerContact, CustomerNote, LeipzigerEntry
from app.models.audit_log import AuditEventType, AuditLog
from app.services import customer_sources
from app.services.customers import CustomerMatcher
from app.services.llm.schemas import ExtractedCustomer
from app.tenancy import bypass_tenant_scope, use_tenant_id
from tests.test_customer_notes import _contacts, _input, _notes, office  # noqa: F401
from tests.test_permissions_security import login, world  # noqa: F401


def _customers(tenant_id, **filters):
    with bypass_tenant_scope():
        return Customer.query.filter_by(tenant_id=tenant_id, **filters).order_by(Customer.id).all()


def _new(office, name="Max Mustermann"):
    return _customers(office.tenant_a.id, name=name)


def test_example_creates_customer_and_note_in_one_go(app, office):
    data = _input(login(app, "laura@example.com"), "808/654321-e max mustermann huk24 wechsel", client_ref="n1")
    assert data["kind"] == "saved" and data["created"] is True
    assert data["message"] == "Max Mustermann wurde angelegt. Notiz gespeichert: HUK24 Wechsel."
    [customer] = _new(office)
    assert customer.customer_number == "808/654321-E" and customer.customer_number_key == "808654321E"
    assert customer.tenant_id == office.tenant_a.id and customer.assigned_user_id == office.laura_id
    assert customer.source == customer_sources.NOTIZ
    # Nicht angegebene Stammdaten bleiben leer.
    assert (customer.phone, customer.email, customer.address, customer.postal_code, customer.city, customer.date_of_birth) == (None,) * 6
    [note] = _notes(customer.id)
    assert note.text == "HUK24 Wechsel" and note.created_by_user_id == office.laura_id and note.created_at
    card = data["card"]
    assert card["id"] == customer.id and card["vn"] == "808/654321-E" and card["latest"]["text"] == "HUK24 Wechsel"
    assert card["leipziger"]["state"] == "none"
    assert _contacts() == []  # keine Rueckruf-Angabe -> kein offener Rueckruf
    with bypass_tenant_scope():
        assert AuditLog.query.filter_by(event_type=AuditEventType.CUSTOMER_CREATED).count() == 1
        assert LeipzigerEntry.query.filter_by(customer_id=customer.id).count() == 0


def test_name_before_number_and_callback_note(app, office):
    data = _input(login(app, "laura@example.com"), "Erika Musterfrau 808/777777-a Rechtsschutz, bitte zurückrufen")
    assert data["created"] is True
    [customer] = _new(office, "Erika Musterfrau")
    assert customer.customer_number == "808/777777-A"
    [contact] = _contacts(customer.id)
    assert contact.topic == "Rechtsschutz" and contact.status == "open"


def test_explicit_create_without_note(app, office):
    data = _input(login(app, "laura@example.com"), "Neuer Kunde 808/555555-k Hans Peter")
    assert data["kind"] == "card" and data["created"] is True and data["message"] == "Hans Peter wurde angelegt."
    assert len(_new(office, "Hans Peter")) == 1
    assert _notes() == []


def test_existing_customer_is_reused(app, office):
    client = login(app, "laura@example.com")
    _input(client, "808/654321-e max mustermann huk24 wechsel")
    data = _input(client, "808/654321-E Max Mustermann nicht erreicht")
    assert data["kind"] == "saved" and not data.get("created")
    assert len(_new(office)) == 1 and len(_notes(_new(office)[0].id)) == 2
    # Bestehender Listen-Kunde (VN nur in der Leipziger Liste): keine Neuanlage.
    data = _input(client, "808/123456-e Justin Heller Kfz-Wechsel")
    assert not data.get("created") and data["card"]["id"] == office.ids["Justin Heller"]
    assert len(_customers(office.tenant_a.id, name="Justin Heller")) == 1


def test_double_submission_creates_one_customer_and_one_note(app, office):
    client = login(app, "laura@example.com")
    first = _input(client, "808/654321-e max mustermann huk24 wechsel", client_ref="dup")
    again = _input(client, "808/654321-e max mustermann huk24 wechsel", client_ref="dup")
    other_ref = _input(client, "808/654321-e max mustermann huk24 wechsel", client_ref="dup2")
    dennis = _input(login(app, "dennis@example.com"), "808/654321-e Max Mustermann huk24 wechsel", client_ref="d")
    assert first["created"] and not again.get("created") and again["duplicate"] is True
    assert other_ref["duplicate"] is True and not other_ref.get("created")
    assert not dennis.get("created")
    assert len(_new(office)) == 1
    assert [n.created_by_user_id for n in _notes(_new(office)[0].id)] == [office.laura_id, office.dennis_id]


def test_same_name_without_number_match_asks_instead_of_creating(app, office):
    client = login(app, "laura@example.com")
    data = _input(client, "808/444444-x Justin Heller HUK24 Wechsel")
    assert data["kind"] == "choose" and data["create_option"] is True
    assert [c["name"] for c in data["candidates"]] == ["Justin Heller"]
    assert len(_customers(office.tenant_a.id, name="Justin Heller")) == 1 and _notes() == []
    # Ausdruecklich "Neuen Kunden anlegen" (zwei Personen koennen gleich heissen).
    created = _input(client, data["create"]["text"], mode="create", create_new=True, new_name=data["create"]["name"])
    assert created["created"] is True
    assert len(_customers(office.tenant_a.id, name="Justin Heller")) == 2
    assert [n.text for n in _notes(created["card"]["id"])] == ["HUK24 Wechsel"]


def test_ambiguous_existing_customers_never_create(app, office):
    client = login(app, "laura@example.com")
    data = _input(client, "Peter Krause HUK24 Wechsel")
    assert data["kind"] == "choose" and not data["create_option"]
    data = _input(client, "808/123456-e Maria Schulz HUK24 Wechsel")
    assert data["kind"] == "conflict"
    assert len(_customers(office.tenant_a.id)) == office.customer_count


def test_missing_name_or_number_is_asked_for(app, office):
    client = login(app, "laura@example.com")
    data = _input(client, "808/654321-e HUK24 Wechsel")
    assert data["kind"] == "need_name"
    created = _input(client, data["pending_note"], new_name="max mustermann", mode="create")
    assert created["created"] and created["message"] == "Max Mustermann wurde angelegt. Notiz gespeichert: HUK24 Wechsel."

    data = _input(client, "Erika Beispiel HUK24 Wechsel, nicht erreicht")
    assert data["kind"] == "need_vn" and data["name"] == "Erika Beispiel"
    assert len(_new(office, "Erika Beispiel")) == 0
    created = _input(client, data["pending_note"], new_vn="808/333444-b", new_name=data["name"], mode="create")
    assert created["created"] is True
    [customer] = _new(office, "Erika Beispiel")
    assert customer.customer_number == "808/333444-B"
    assert [n.text for n in _notes(customer.id)] == ["HUK24 Wechsel, nicht erreicht"]
    assert _contacts(customer.id)[0].topic == "HUK24 Wechsel"

    status = client.post("/zentriq/api/kunden/eingabe", json={"text": "x", "new_vn": "12345", "mode": "create"}).status_code
    assert status == 400


def test_pure_search_never_creates(app, office):
    client = login(app, "laura@example.com")
    for text in ("808/654321-e", "808/654321-e Max Mustermann", "Was ist bei 808/654321-e offen?", "Max Mustermann"):
        data = _input(client, text)
        assert data["kind"] in ("not_found", "chat")
    data = _input(client, "808/654321-e Max Mustermann")
    assert data["create"]["name"] == "Max Mustermann"  # Anlegen wird nur angeboten
    assert _new(office) == [] and _notes() == []


def test_new_customer_found_by_search_and_list_matching(app, office, db):
    client = login(app, "laura@example.com")
    _input(client, "808/654321-e max mustermann huk24 wechsel")
    [customer] = _new(office)
    for query in ("Max Mustermann", "808/654321-E", "Mustermann"):
        assert _input(login(app, "dennis@example.com"), query)["card"]["id"] == customer.id
    admin = login(app, "admin-a@example.com")
    assert "Max Mustermann" in admin.get("/search?q=808654321").get_data(as_text=True)
    assert "Max Mustermann" in admin.get("/customers?q=Mustermann").get_data(as_text=True)
    # Naechster Listenimport: Vorgang 808/654321-E-12 "Mustermann, Max" (mit Geburtsdatum/PLZ)
    # wird dem angelegten Kunden zugeordnet statt einen zweiten anzulegen.
    with use_tenant_id(office.tenant_a.id):
        matcher = CustomerMatcher()
        row = ExtractedCustomer(name="Mustermann, Max", postal_code="96450", city="Coburg", date_of_birth=date(1980, 1, 2))
        assert matcher.find(row, "808/654321-E-12").id == customer.id
        assert matcher.find(ExtractedCustomer(name="Max Mustermann"), "808/999999-A-12") is None


def test_office_boundaries_for_creation(app, office):
    # Dieselbe VN in Buero B: Laura (Buero A) legt einen eigenen Kunden in A an, sieht B nie.
    _input(login(app, "bob@example.com"), "808/654321-e max mustermann huk24 wechsel")
    data = _input(login(app, "laura@example.com"), "808/654321-e max mustermann huk24 wechsel")
    assert data["created"] is True
    assert len(_customers(office.tenant_a.id, name="Max Mustermann")) == 1
    assert len(_customers(office.tenant_b.id, name="Max Mustermann")) == 1
    with bypass_tenant_scope():
        a = _customers(office.tenant_a.id, name="Max Mustermann")[0]
        assert {n.tenant_id for n in CustomerNote.query.filter_by(customer_id=a.id)} == {office.tenant_a.id}
        assert CustomerContact.query.count() == 0
    root = login(app, "justin@example.com")
    assert root.post("/zentriq/api/kunden/eingabe", json={"text": "808/111222-a Neu Kunde HUK24"}).status_code == 403


def test_note_words_are_not_taken_as_name(app, office):
    client = login(app, "laura@example.com")
    for text in ("808/654321-e Police verschickt heute", "808/654321-e Unterlagen angefordert", "808/654321-e Kunde ruft morgen zurück"):
        assert _input(client, text)["kind"] == "need_name", text
    assert _new(office, "Police Verschickt") == [] and _notes() == []
