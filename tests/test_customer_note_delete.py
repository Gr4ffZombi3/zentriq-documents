# ruff: noqa: F811  (Fixtures werden aus anderen Testmodulen importiert)
"""Kundennotizen loeschen (Drei-Punkte-Menue der Kundenkarte) und neue Kartenangaben.

Buero A: Admin A, Dennis und Laura (Mitarbeiter); Buero B: Bob. Geloescht wird nur ueber die
konkrete Notiz-ID; Buero-Admin jede Notiz, Mitarbeiter nur eigene. Synthetische Daten."""

from datetime import timedelta

import pytest

from app.models import AuditLog, CustomerNote
from app.models.audit_log import AuditEventType
from app.services import customer_notes
from app.services.ai import adapters, router
from app.tenancy import bypass_tenant_scope
from tests.test_customer_notes import _customer_list, _row
from tests.test_permissions_security import login, world  # noqa: F401

API = "/zentriq/api"


@pytest.fixture()
def office(app, db, world):
    _doc, ids = _customer_list(db, world.tenant_a.id, [("Max Mustermann", "0228 1234567", [_row("808/100001-A", "08/5555-B")]),
                                                       ("Erika Muster", None, [])])
    _doc_b, ids_b = _customer_list(db, world.tenant_b.id, [("Olga Fremd", None, [])], "b.pdf")
    world.max = ids["Max Mustermann"]
    world.erika = ids["Erika Muster"]
    world.olga = ids_b["Olga Fremd"]
    return world


@pytest.fixture()
def no_model(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Modellaufruf ist hier nicht erlaubt")

    monkeypatch.setattr(router, "can_answer", lambda: True)
    monkeypatch.setattr(adapters, "call", forbidden)
    monkeypatch.setattr(router, "complete", forbidden)


def _note(client, customer_id, text):
    resp = client.post(f"{API}/kunden/eingabe", json={"text": text, "customer_id": customer_id, "mode": "note"})
    assert resp.status_code == 200, resp.get_json()
    return resp.get_json()


def _notes(customer_id):
    with bypass_tenant_scope():
        return CustomerNote.query.filter_by(customer_id=customer_id).order_by(CustomerNote.id).all()


def _age(db, note_id, minutes):
    with bypass_tenant_scope():
        note = db.session.get(CustomerNote, note_id)
        note.created_at = note.created_at - timedelta(minutes=minutes)
        db.session.commit()


def _delete(client, note_id, customer_id):
    resp = client.delete(f"{API}/notizen/{note_id}", json={"customer_id": customer_id})
    return resp.status_code, resp.get_json()


def test_card_has_count_and_newest_first(app, db, office):
    laura = login(app, "laura@example.com")
    first = _note(laura, office.max, "Erste Notiz")["card"]["notes"][0]["id"]
    _age(db, first, 10)
    card = _note(laura, office.max, "Zweite Notiz")["card"]
    assert card["note_count"] == 2
    assert [n["text"] for n in card["notes"]] == ["Zweite Notiz", "Erste Notiz"]
    assert len({n["id"] for n in card["notes"]}) == 2  # jede Notiz genau einmal
    assert all(n["can_delete"] for n in card["notes"])  # eigene Notizen
    assert card["leipziger"]["state"] == "found"


def test_delete_newest_note_moves_next_up_and_logs_without_text(app, db, office):
    laura = login(app, "laura@example.com")
    first = _note(laura, office.max, "Ältere Notiz")["card"]["notes"][0]["id"]
    _age(db, first, 10)
    newest = _note(laura, office.max, "Neueste Notiz mit Geheimtext")["card"]["notes"][0]["id"]
    status, data = _delete(laura, newest, office.max)
    assert status == 200 and data["kind"] == "deleted", data
    card = data["card"]
    assert card["note_count"] == 1 and card["notes"][0]["id"] == first and card["latest"]["id"] == first
    assert [n.id for n in _notes(office.max)] == [first]
    with bypass_tenant_scope():
        entry = AuditLog.query.filter_by(event_type=AuditEventType.CUSTOMER_NOTE_DELETED).one()
    assert entry.details["note_id"] == newest and entry.details["customer_id"] == office.max
    assert "Geheimtext" not in str(entry.details)


def test_delete_only_note_leaves_empty_card(app, office):
    laura = login(app, "laura@example.com")
    only = _note(laura, office.max, "Einzige Notiz")["card"]["notes"][0]["id"]
    status, data = _delete(laura, only, office.max)
    assert status == 200
    assert data["card"]["note_count"] == 0 and data["card"]["notes"] == [] and data["card"]["latest"] is None
    # schon geloescht -> verstaendliche Meldung, nichts passiert
    status, data = _delete(laura, only, office.max)
    assert status == 404 and "schon gelöscht" in data["error"]


def test_employee_cannot_delete_colleagues_note_admin_can(app, office):
    laura = login(app, "laura@example.com")
    note_id = _note(laura, office.max, "Notiz von Laura")["card"]["notes"][0]["id"]
    dennis = login(app, "dennis@example.com")
    card = dennis.get(f"{API}/kunden/{office.max}").get_json()["card"]
    assert card["notes"][0]["can_delete"] is False
    status, data = _delete(dennis, note_id, office.max)
    assert status == 403 and "Verfasser oder ein Büro-Admin" in data["error"]
    assert [n.id for n in _notes(office.max)] == [note_id]
    admin = login(app, "admin-a@example.com")
    assert admin.get(f"{API}/kunden/{office.max}").get_json()["card"]["notes"][0]["can_delete"] is True
    status, _data = _delete(admin, note_id, office.max)
    assert status == 200 and _notes(office.max) == []


def test_wrong_customer_and_other_office_are_rejected(app, office):
    laura = login(app, "laura@example.com")
    note_id = _note(laura, office.max, "Notiz zu Max")["card"]["notes"][0]["id"]
    status, data = _delete(laura, note_id, office.erika)
    assert status == 409 and "nicht zu diesem Kunden" in data["error"]
    bob = login(app, "bob@example.com")
    assert _delete(bob, note_id, office.max)[0] == 404
    assert login(app, "admin-b@example.com").delete(f"{API}/notizen/{note_id}", json={}).status_code == 404
    assert login(app, "justin@example.com").delete(f"{API}/notizen/{note_id}", json={}).status_code == 403
    assert len(_notes(office.max)) == 1


def test_delete_keeps_open_callback(app, office):
    laura = login(app, "laura@example.com")
    data = _note(laura, office.max, "nicht erreicht wegen HUK24 Wechsel")
    assert data["card"]["open_contacts"]
    status, data = _delete(laura, data["card"]["notes"][0]["id"], office.max)
    assert status == 200 and data["card"]["open_contacts"] and data["card"]["note_count"] == 0


def test_all_notes_endpoint_beyond_card_limit(app, db, office, monkeypatch):
    monkeypatch.setattr(customer_notes, "HISTORY_LIMIT", 2)
    laura = login(app, "laura@example.com")
    for i in range(4):
        note_id = _note(laura, office.max, f"Notiz {i}")["card"]["notes"][0]["id"]
        _age(db, note_id, 10 - i)
    card = laura.get(f"{API}/kunden/{office.max}").get_json()["card"]
    assert card["note_count"] == 4 and len(card["notes"]) == 2 and card["more_notes"] is True
    data = laura.get(f"{API}/kunden/{office.max}/notizen").get_json()
    assert [n["text"] for n in data["notes"]] == ["Notiz 3", "Notiz 2", "Notiz 1", "Notiz 0"]
    assert login(app, "bob@example.com").get(f"{API}/kunden/{office.max}/notizen").status_code == 404


@pytest.mark.parametrize("text", ["Letzte Notiz löschen", "Lösche die letzte Notiz", "Notiz löschen", "letzte notiz loeschen"])
def test_delete_request_in_assistant_field_only_shows_hint(app, office, no_model, text):
    laura = login(app, "laura@example.com")
    _note(laura, office.max, "Bleibt bestehen")
    for extra in ({}, {"selected_id": office.max}):
        resp = laura.post(f"{API}/kunden/eingabe", json={"text": text, **extra})
        data = resp.get_json()
        assert resp.status_code == 200 and data["kind"] == "facts", data
        assert data["message"] == "Wähle die gewünschte Notiz über das Drei-Punkte-Menü aus."
    assert [n.text for n in _notes(office.max)] == ["Bleibt bestehen"]


def test_note_form_saves_free_text_unchanged(app, office, no_model):
    laura = login(app, "laura@example.com")
    data = _note(laura, office.max, "Letzte Notiz löschen")
    assert data["kind"] == "saved"
    assert [n.text for n in _notes(office.max)] == ["Letzte Notiz löschen"]
