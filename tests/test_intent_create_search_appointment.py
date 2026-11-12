"""Assistent: Kunde anlegen, Kunde suchen und Termin anlegen werden nicht verwechselt.

Ueber den normalen Einstieg (/zentriq/api/kunden/eingabe), KI-Modell verfuegbar (wie Produktion), jeder
Modellaufruf wird gezaehlt. Geprueft wird jeweils, welche Kunden, Termine und Notizen tatsaechlich
entstehen. Anonymisierte Daten."""

from datetime import timedelta

import pytest

from app.models import CalendarEvent, Customer, CustomerNote, EmployeeProfile
from app.models.office_settings import OfficeSettings
from app.services import customer_create
from app.services.ai import router

API = "/zentriq/api/kunden/eingabe"
HOURS = {str(d): ["08:00", "18:00"] for d in range(1, 8)}


@pytest.fixture()
def model(monkeypatch):
    calls = []

    class Fake:
        text = '{"anlegen": true, "vorname": "Max", "nachname": "Mustermann", "ort": "Bonn"}'
        provider = model = model_label = "fake"
        tools_used = []
        input_tokens = output_tokens = 0
        cost_usd, cached, fallback_used = 0, False, False

    def complete(**kwargs):
        calls.append(kwargs.get("purpose"))
        return Fake()

    monkeypatch.setattr(router, "complete", complete)
    monkeypatch.setattr(router, "can_answer", lambda: True)
    return calls


@pytest.fixture()
def office(db, tenant, user, employee):
    db.session.add_all([
        EmployeeProfile(tenant_id=tenant.id, user_id=employee.id, display_name="Laura Test"),
        EmployeeProfile(tenant_id=tenant.id, user_id=user.id, display_name="Anna Admin"),
        OfficeSettings(tenant_id=tenant.id, hours=HOURS, default_minutes=45, buffer_minutes=0, lead_minutes=0, horizon_days=21),
    ])
    karl = Customer(tenant_id=tenant.id, name="Karl Schmidthofer", assigned_user_id=employee.id)
    erika = Customer(tenant_id=tenant.id, name="Erika Beispiel")
    db.session.add_all([karl, erika])
    db.session.commit()
    return {"karl": karl.id, "erika": erika.id}


def _say(client, text, **extra):
    resp = client.post(API, json={"text": text, **extra})
    assert resp.status_code == 200, resp.get_json()
    return resp.get_json()


def _created():
    return Customer.query.filter(Customer.name == "Max Mustermann").count()


def _counts():
    return Customer.query.count(), CalendarEvent.query.count(), CustomerNote.query.count()


@pytest.mark.parametrize("text", [
    "Lege Kunde Max Mustermann an", "Neuer Kunde Max Mustermann", "Nimm Max Mustermann als Kunden auf",
    "lege kunde max mustermann an", "lge kunde max mustermann an", "Kannst du Max Mustermann neu anlegen?",
    "Max Mustermann neu anlegen", "Max Mustermann aufnehmen", "Leg mal Max Mustermann an",
    "Bitte Max Mustermann als neuen Kunden erfassen", "Trag Max Mustermann als Kunde ein",
])
def test_clear_creation_saves_at_once_locally(auth_client, office, model, text):
    before = _counts()
    r = _say(auth_client, text)
    assert r["kind"] == "card" and r.get("created") and r["message"] == "Max Mustermann wurde angelegt.", (text, r)
    assert _created() == 1 and model == []  # lokal, kein Modellaufruf
    assert _counts() == (before[0] + 1, before[1], before[2])  # kein Termin, keine Notiz


def test_free_creation_uses_exactly_one_model_call(auth_client, office, model):
    r = _say(auth_client, "Kunde Max Mustermann, wohnhaft Bonn, neu aufnehmen bitte")
    assert r["kind"] == "card" and r.get("created")
    assert model == ["customer_create"] and _created() == 1


def test_creation_with_selected_other_customer_still_creates(auth_client, office, model):
    r = _say(auth_client, "Nimm Max Mustermann als Kunden auf", selected_id=office["karl"])
    assert r["kind"] == "card" and r.get("created") and _created() == 1
    assert CustomerNote.query.count() == 0


def test_missing_name_is_asked_and_typed_answer_continues(auth_client, office, model):
    q = _say(auth_client, "Neuer Kunde")
    assert q["kind"] == "create_name" and q["await_create"]
    r = _say(auth_client, "Max Mustermann", create_pending=q["await_create"])
    assert r["kind"] == "card" and r.get("created") and _created() == 1 and model == []


def test_duplicate_asks_instead_of_silently_searching(auth_client, office, model, db):
    erika = db.session.get(Customer, office["erika"])
    erika.created_at = erika.created_at - timedelta(days=30)  # vorhandener Kunde (kein Doppelklick)
    db.session.commit()
    r = _say(auth_client, "Lege Kunde Erika Beispiel an")
    assert r["kind"] == "choose" and "bereits angelegt" in r["message"] and r["create_option"] is True
    assert Customer.query.filter_by(name="Erika Beispiel").count() == 1
    create = r["create"]  # wie der Knopf "Neuen Kunden ... anlegen" (js/customer-notes.js createButton)
    r = _say(auth_client, create["text"], mode="create", create_new=True, new_name=create["name"],
             first_name=create["first_name"], last_name=create["last_name"])
    assert r["kind"] == "card" and r.get("created"), r
    assert Customer.query.filter_by(name="Erika Beispiel").count() == 2  # ausdruecklich trotzdem neu angelegt


@pytest.mark.parametrize("text", ["Such Max Mustermann", "Zeig mir die Akte von Max Mustermann", "Öffne Max Mustermann"])
def test_explicit_search_only_searches(auth_client, office, model, text):
    before = _counts()
    r = _say(auth_client, text)
    assert r["kind"] == "not_found" and not r.get("create"), (text, r)
    assert _counts() == before and model == []
    _say(auth_client, "Lege Kunde Max Mustermann an")
    assert _say(auth_client, text)["kind"] == "card"  # mit Treffer: Karte


def test_bare_name_without_match_asks_search_or_create(auth_client, office, model):
    r = _say(auth_client, "Max Mustermann")
    assert r["kind"] == "not_found" and "Kunden suchen oder neu anlegen?" in r["message"]
    assert r["create"]["first_name"] == "Max" and r["create"]["last_name"] == "Mustermann"
    assert _created() == 0 and model == []


def test_appointment_never_creates_customer_or_note(auth_client, office, model):
    before = _counts()
    for text in ("Mach für Max Mustermann Freitag um 11 einen Termin", "Lege einen Termin für Max Mustermann am Freitag um 11 an",
                 "Leg für Max Mustermann Freitag um 11 einen Termin an"):
        r = _say(auth_client, text)
        assert r["kind"] == "calendar_question", (text, r)  # unbekannter Kunde -> gezielte Rueckfrage
    assert _counts() == before and model == []


def test_appointment_for_selected_customer_and_conflict(auth_client, office, model):
    r = _say(auth_client, "Leg für den ausgewählten Kunden einen Termin fest", selected_id=office["karl"])
    assert r["kind"] == "calendar_question" and "Karl Schmidthofer" in r["message"] and r["await_when"]
    r = _say(auth_client, "Mach für Erika Beispiel Freitag um 11 einen Termin", selected_id=office["karl"])
    assert r["kind"] == "calendar_question" and "ausgewählt ist Karl Schmidthofer" in r["message"]
    assert _counts()[1:] == (0, 0)


def test_help_question_is_not_a_creation():
    assert customer_create.creation_route("Wie lege ich einen Kunden an?") is None
    assert customer_create.creation_route("Max Mustermann Vertrag anlegen") is None
    assert customer_create.creation_route("Mach für Max Mustermann Freitag um 11 einen Termin") is None
