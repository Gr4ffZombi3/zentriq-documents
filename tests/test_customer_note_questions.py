# ruff: noqa: F811  (Fixtures werden aus anderen Testmodulen importiert)
"""Suchfragen ("Was hat 0176... gesagt?") ueber das ZentriqAI-Feld: Vorrang vor dem Notizmodus,
Telefonnummernsuche, Antworten nur aus gespeicherten Notizen, keine Schreibzugriffe."""

import pytest

from app.extensions import db
from app.models import AuditLog, Customer, CustomerContact, CustomerNote
from app.services.ai import router
from app.tenancy import bypass_tenant_scope, use_tenant_id
from tests.test_customer_notes import _input, office  # noqa: F401
from tests.test_permissions_security import login, world  # noqa: F401


@pytest.fixture(autouse=True)
def no_ai(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Externer KI-Aufruf bei einer Standardfrage")

    monkeypatch.setattr(router, "complete", forbidden)


def _snapshot():
    """Stand aller Daten, die eine Leseanfrage nie aendern darf."""
    with bypass_tenant_scope():
        db.session.expire_all()
        return (
            [(n.id, n.text, n.event, n.contact_id) for n in CustomerNote.query.order_by(CustomerNote.id)],
            [(c.id, c.status, c.outcome, c.attempts, c.updated_at) for c in CustomerContact.query.order_by(CustomerContact.id)],
            [(c.id, c.name, c.phone, c.customer_number, c.updated_at) for c in Customer.query.order_by(Customer.id)],
            AuditLog.query.filter(AuditLog.event_type != AuditLog.event_type.type.python_type.LOGIN_SUCCESS).count(),
        )


def _set_phone(customer_id, phone):
    with bypass_tenant_scope():
        customer = db.session.get(Customer, customer_id)
        customer.phone = phone
        db.session.commit()


@pytest.fixture()
def with_notes(app, office):
    laura = login(app, "laura@example.com")
    _input(laura, "808/123456-e Justin Heller HUK24 Wechsel")
    _input(laura, "808/111111-b Maria Schulz Kunde möchte Angebot für Hausrat")
    return office


@pytest.mark.parametrize(
    "question", ["was hat 0176123456 gesagt", "Was hat +49 176 123456 gesagt?", "Was hat 0176/12 34 56 gesagt?"]
)
def test_phone_question_finds_customer_by_stored_number(app, with_notes, question):
    _set_phone(with_notes.ids["Maria Schulz"], "0176 123456")
    client = login(app, "dennis@example.com")
    before = _snapshot()
    data = _input(client, question, selected_id=with_notes.ids["Justin Heller"])
    assert data["kind"] == "card" and data["card"]["id"] == with_notes.ids["Maria Schulz"]
    answer = data["answer"]
    assert answer["text"] == "Dokumentierte Aussagen von Maria Schulz:"
    assert answer["sources"][0]["text"] == "Kunde möchte Angebot für Hausrat"
    assert answer["sources"][0]["source"] == "Kundenaussage" and answer["sources"][0]["author"] == "Laura"
    assert _snapshot() == before


def test_unknown_phone_number(app, with_notes):
    client = login(app, "laura@example.com")
    before = _snapshot()
    data = _input(client, "was hat 0176123456 gesagt", selected_id=with_notes.ids["Justin Heller"])
    assert data == {"kind": "not_found", "search": True, "message": "Zu dieser Telefonnummer ist kein Kunde hinterlegt."}
    assert "gespeichert" not in data["message"]
    # Nummer wird nicht dem ausgewaehlten Kunden zugeordnet und nirgends gespeichert.
    assert _snapshot() == before
    with bypass_tenant_scope():
        assert db.session.get(Customer, with_notes.ids["Justin Heller"]).phone == "0171 1234567"


def test_no_fuzzy_phone_match(app, with_notes):
    _set_phone(with_notes.ids["Maria Schulz"], "0176 1234567")
    data = _input(login(app, "laura@example.com"), "was hat 0176123456 gesagt")
    assert data["kind"] == "not_found"


def test_internal_reason_is_not_presented_as_customer_statement(app, with_notes):
    data = _input(login(app, "laura@example.com"), "was hat 0171 1234567 gesagt")
    assert data["card"]["id"] == with_notes.ids["Justin Heller"]
    assert data["answer"]["text"] == "Zu Justin Heller ist keine Aussage des Kunden dokumentiert. Vorhandene interne Notizen:"
    assert data["answer"]["sources"] == [
        {"text": "HUK24 Wechsel", "author": "Laura", "when": data["answer"]["sources"][0]["when"], "source": "Interne Notiz"}
    ]


def test_customer_without_notes(app, with_notes):
    _set_phone(with_notes.ids["Anna Fischer"], "+49 170 5550000")
    data = _input(login(app, "laura@example.com"), "Was hat 0170 5550000 gesagt?")
    assert data["card"]["id"] == with_notes.ids["Anna Fischer"]
    assert data["answer"] == {"text": "Zu Anna Fischer ist keine Gesprächsnotiz dokumentiert.", "sources": []}
    data = _input(login(app, "laura@example.com"), "Warum wurde Anna Fischer angerufen?")
    assert data["answer"]["text"] == "Zu Anna Fischer ist keine Gesprächsnotiz dokumentiert."


def test_multiple_customers_with_same_phone(app, with_notes):
    _set_phone(with_notes.ids["Peter Krause"], "0176 123456")
    _set_phone(with_notes.ids["Peter Krause 2"], "+49176123456")
    client = login(app, "laura@example.com")
    before = _snapshot()
    data = _input(client, "was hat 0176123456 gesagt", selected_id=with_notes.ids["Justin Heller"])
    assert data["kind"] == "choose" and len(data["candidates"]) == 2 and data["pending_note"] is None
    picked = _input(client, "was hat 0176123456 gesagt", customer_id=data["candidates"][0]["id"], mode="search")
    assert picked["kind"] == "card" and picked["answer"]["text"].startswith("Zu Peter Krause ist keine Gesprächsnotiz")
    assert _snapshot() == before


@pytest.mark.parametrize(
    "question",
    [
        "Was hat er gesagt?",
        "Warum wurde er angerufen?",
        "Was ist noch offen?",
        "Was hat 0171 1234567 gesagt?",
        "Was ist bei 808/111111-B offen?",
        "warum hat laura 808123456e angerufen",
        "Was hat 0176999999 gesagt?",
        "Was ist bei 808/999999-Z offen?",
    ],
)
def test_questions_never_write_even_with_selected_card(app, with_notes, question):
    client = login(app, "laura@example.com")
    _input(client, "808/123456-e nicht erreicht")  # offener Vorgang beim ausgewaehlten Kunden
    before = _snapshot()
    data = _input(client, question, selected_id=with_notes.ids["Justin Heller"])
    assert data["kind"] in ("card", "not_found")
    if data["kind"] == "not_found":
        assert "gespeichert" not in data["message"] and data["search"] is True
    assert _snapshot() == before


def test_selected_card_question_answers_for_selected_customer(app, with_notes):
    client = login(app, "laura@example.com")
    data = _input(client, "Was hat sie gesagt?", selected_id=with_notes.ids["Maria Schulz"])
    assert data["card"]["id"] == with_notes.ids["Maria Schulz"]
    assert data["answer"]["sources"][0]["source"] == "Kundenaussage"


def test_phone_from_customer_record_is_used_and_other_office_is_ignored(app, with_notes):
    # Telefonnummer am Kunden in Buero B: fuer Buero A unsichtbar.
    with use_tenant_id(with_notes.tenant_b.id):
        _set_phone(with_notes.heller_b, "0176 123456")
    data = _input(login(app, "laura@example.com"), "was hat 0176123456 gesagt")
    assert data["kind"] == "not_found"
    data = _input(login(app, "bob@example.com"), "was hat 0176123456 gesagt")
    assert data["card"]["id"] == with_notes.heller_b
