# ruff: noqa: F811  (Fixtures werden aus anderen Testmodulen importiert)
"""Hauptfeld entscheidet selbst: eindeutige Bueroaufgaben direkt (ohne Modell), gemischte Aufgaben
mit Backend-Daten und nur dem Sprachanteil fuer das Modell, unklare Schreibauftraege mit Rueckfrage.
Synthetische Daten; KI-Anbieter aus, jeder Modellaufruf ist ein Testfehler."""

from datetime import timedelta

import pytest

from app.extensions import db as _db
from app.models import CalendarEvent, Customer, CustomerContact, CustomerNote
from app.models.ai import AiMessage
from app.models.office_settings import OfficeSettings
from app.services.ai import adapters, router
from app.services.timetracking.clock import local_today
from app.tenancy import bypass_tenant_scope
from tests.conftest import next_workday
from tests.test_customer_notes import API, _input, office  # noqa: F401
from tests.test_permissions_security import login, world  # noqa: F401


@pytest.fixture()
def no_ai(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Modellaufruf ist hier nicht erlaubt")

    monkeypatch.setattr(adapters, "call", forbidden)
    assert not router.can_answer()


@pytest.fixture()
def karl(app, office):
    with bypass_tenant_scope():
        t = office.tenant_a
        _db.session.add(OfficeSettings(tenant_id=t.id, hours={str(d): ["08:00", "18:00"] for d in range(1, 8)}, default_minutes=45, lead_minutes=0))
        customer = Customer(tenant_id=t.id, name="Karl Schmidthofer", name_key="karl schmidthofer", assigned_user_id=office.laura_id)
        _db.session.add(customer)
        _db.session.commit()
        return customer.id


def _chat(client, text, **extra):
    conversation = client.post(API + "/conversations", json={}).get_json()["conversation"]["id"]
    sent = client.post(f"{API}/conversations/{conversation}/messages", json={"content": text, **extra}).get_json()
    return client.get(f"{API}/conversations/{conversation}/messages/{sent['reply']['id']}").get_json()["message"]


def test_typo_in_search_offers_suggestion_not_assignment(app, karl, no_ai):
    data = _input(login(app, "admin-a@example.com"), "zeig mir schmidhofer")
    assert data["kind"] == "choose" and "Meinten Sie" in data["message"]
    assert [c["id"] for c in data["candidates"]] == [karl]


def test_note_command_saves_at_unique_customer_and_opens_callback(app, karl, no_ai):
    client = login(app, "laura@example.com")
    data = _input(client, "Bei Schmidthofer notieren: wegen HUK24 nicht erreicht")
    assert data["kind"] == "saved" and "Karl Schmidthofer" in data["message"]
    with bypass_tenant_scope():
        note = CustomerNote.query.filter_by(customer_id=karl).one()
        contact = CustomerContact.query.filter_by(customer_id=karl).one()
    assert "HUK24" in note.text and contact.status == "open"
    assert _input(client, "Wie viele Rückrufe sind offen?")["count"] == 1


def test_note_command_with_ambiguous_name_keeps_note(app, karl, office, no_ai):
    with bypass_tenant_scope():
        _db.session.add(Customer(tenant_id=office.tenant_a.id, name="Maria Schmidthofer", name_key="maria schmidthofer"))
        _db.session.commit()
    data = _input(login(app, "laura@example.com"), "Bei Schmidthofer notieren: Unterlagen angefordert")
    assert data["kind"] == "choose" and data["pending_note"] == "Unterlagen angefordert" and len(data["candidates"]) == 2
    with bypass_tenant_scope():
        assert CustomerNote.query.count() == 0


def test_short_variants_lowercase_and_date_without_dot(app, karl, no_ai):
    client = login(app, "admin-a@example.com")
    day = next_workday(2)
    booked = _input(client, f"{day.day}.{day.month}. schmidthofer termin 17 uhr")
    assert booked["kind"] == "calendar_saved" and "Karl Schmidthofer" in booked["message"]
    free = _input(client, f"Ist am {day.day}.{day.month} um 17 Uhr bei Laura frei")
    assert free["kind"] == "calendar_list" and "nicht frei" in free["message"] and "nichts gebucht" in free["message"]
    with bypass_tenant_scope():
        assert CalendarEvent.query.count() == 1


def test_mixed_reply_for_named_customer_uses_calendar_and_template(app, karl, no_ai):
    client = login(app, "admin-a@example.com")
    assert _input(client, "Schreib Schmidthofer eine freundliche Antwort mit drei freien Terminen")["customer_id"] == karl
    reply = _chat(client, "Schreib Schmidthofer eine freundliche Antwort mit drei freien Terminen")
    text = reply["content"]
    assert reply["status"] == "done" and "Kunde: Karl Schmidthofer" in text and "3 freie Termine bei Laura" in text
    assert text.count("\n- ") >= 3 and "nicht gesendet, kein Termin gebucht" in text
    with bypass_tenant_scope():
        assert CalendarEvent.query.count() == 0


def test_mixed_reply_for_selected_customer(app, karl, no_ai):
    client = login(app, "admin-a@example.com")
    routed = _input(client, "Schreib dem Kunden eine freundliche Antwort mit drei freien Terminen", selected_id=karl)
    assert routed["kind"] == "chat" and routed["customer_id"] == karl
    reply = _chat(client, "Schreib dem Kunden eine freundliche Antwort mit drei freien Terminen", customer_id=karl)
    assert "Kunde: Karl Schmidthofer" in reply["content"] and "Guten Tag Karl Schmidthofer" in reply["content"]


def test_reply_without_customer_gives_neutral_draft(app, karl, no_ai):
    # Freie Termine kommen aus dem Buerokalender - dafuer ist keine Kundenakte noetig (keine Rueckfrage).
    reply = _chat(login(app, "admin-a@example.com"), "Schreib dem Kunden eine freundliche Antwort mit drei freien Terminen")
    assert reply["status"] == "done" and "```E-Mail-Entwurf" in reply["content"] and "Guten Tag," in reply["content"]
    assert "Für welchen Kunden" not in reply["content"]


def test_foreign_customer_context_is_ignored(app, karl, office, no_ai):
    with bypass_tenant_scope():
        foreign = Customer(tenant_id=office.tenant_b.id, name="Fremd Kunde", name_key="fremd kunde")
        _db.session.add(foreign)
        _db.session.commit()
        foreign_id = foreign.id
    reply = _chat(login(app, "admin-a@example.com"), "Schreib dem Kunden eine Antwort mit drei freien Terminen", customer_id=foreign_id)
    assert "Fremd" not in reply["content"] and "Guten Tag," in reply["content"]


def test_vague_writing_order_asks_which_result(app, karl, no_ai):
    client = login(app, "admin-a@example.com")
    data = _input(client, "Schreib was zu Schmidthofer")
    assert data["kind"] == "clarify" and data["customer"]["id"] == karl
    assert _input(client, "Erkläre mir Teilkasko")["kind"] == "no_ai"  # Frage, kein Schreibauftrag


def test_model_proposed_note_is_checked_and_saved_only_after_confirm(app, karl, office):
    from app.models import User
    from app.services.ai import actions, office_tools

    with bypass_tenant_scope():
        laura = _db.session.get(User, office.laura_id)
        foreign = Customer(tenant_id=office.tenant_b.id, name="Fremd Kunde", name_key="fremd kunde")
        _db.session.add(foreign)
        _db.session.commit()
    from app.tenancy import use_tenant_id

    with use_tenant_id(office.tenant_a.id):
        assert "fehler" in office_tools._prepare_note(laura, {"kunde_id": foreign.id, "text": "x"})
        proposal = office_tools._prepare_note(laura, {"kunde_id": karl, "text": "wegen Kfz nicht erreicht"})["aktion"]
        assert CustomerNote.query.count() == 0
        action = actions.prepare(laura, None, proposal["tool"], proposal["arguments"], proposal["summary"])
        done = actions.confirm(laura, action.id)
        assert done.status == "done" and "Karl Schmidthofer" in done.result
        assert CustomerNote.query.filter_by(customer_id=karl).count() == 1
        assert CustomerContact.query.filter_by(customer_id=karl, status="open").count() == 1


def test_model_booking_proposal_uses_office_rules(app, karl, office):
    from app.models import User
    from app.services.ai import office_tools
    from app.tenancy import use_tenant_id

    with bypass_tenant_scope():
        admin = _db.session.get(User, office.admin_a_id)
    day = (local_today() + timedelta(days=3)).isoformat()
    with use_tenant_id(office.tenant_a.id):
        proposal = office_tools._prepare_booking(admin, {"datum": day, "uhrzeit": "11:00", "kunde_id": karl})["aktion"]
    assert proposal["arguments"]["dauer_minuten"] == 45 and proposal["arguments"]["mitarbeiter_id"] == office.laura_id
    with bypass_tenant_scope():
        assert AiMessage.query.count() == 0


def test_count_question_ignores_stale_selected_customer(app, karl, no_ai):
    client = login(app, "laura@example.com")
    _input(client, "Bei Schmidthofer notieren: wegen HUK24 nicht erreicht")
    data = _input(client, "Wie viele Rückrufe sind offen?", selected_id=karl)
    assert data["kind"] == "facts" and data["count"] == 1
