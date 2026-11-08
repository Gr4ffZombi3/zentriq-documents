"""Kundenkarte als Arbeitsflaeche und kurze Folgeeingaben ("Kuerzer", "Doch um 16:30",
"Die Strasse ist falsch") - im Backend mit 0 Modellaufrufen. Schwerpunkt: Kunde, offener Auftrag
und Chat duerfen nicht verwechselt werden; eine alte Karte im Verlauf ist keine Auswahl."""

from datetime import time, timedelta

import pytest

from app.extensions import db as _db
from app.models import CalendarEvent, Customer, CustomerNote, OfficeSettings, Tenant, User, UserRole
from app.models.ai import AiConversation, AiMessage
from app.services.ai import router
from app.services.timetracking.clock import local_to_utc_naive, local_today, to_local
from app.tenancy import bypass_tenant_scope

API = "/zentriq/api"


@pytest.fixture()
def model_calls(monkeypatch):
    calls = []
    monkeypatch.setattr(router, "can_answer", lambda: True)
    monkeypatch.setattr(router, "complete", lambda **k: calls.append(k) or (_ for _ in ()).throw(router.AiError("x", [], "x")))
    return calls


@pytest.fixture()
def office(app, tenant, user):
    _db.session.add(OfficeSettings(tenant_id=tenant.id, office_name="Büro Test", email="info@buero-test.de",
                                   hours={str(d): ["08:00", "18:00"] for d in range(1, 8)}, default_minutes=30, buffer_minutes=0,
                                   lead_minutes=0, horizon_days=21))
    _db.session.commit()
    return tenant


def _c(tenant, name, **kw):
    c = Customer(tenant_id=tenant.id, name=name, **kw)
    _db.session.add(c)
    _db.session.commit()
    return c


def _ev(tenant, user, customer, days, hour, minute=0, mins=45, title="Beratung", kind="vor_ort", location="Büro Bonn"):
    start = local_to_utc_naive(local_today() + timedelta(days=days), time(hour, minute))
    e = CalendarEvent(tenant_id=tenant.id, title=title, starts_at=start, ends_at=start + timedelta(minutes=mins), assignee_user_id=user.id,
                      customer_id=customer.id if customer else None, location_kind=kind, location=location, created_by_user_id=user.id)
    _db.session.add(e)
    _db.session.commit()
    return e


def _say(client, text, cid=None, **extra):
    body = {"text": text, **extra}
    if cid:
        body["conversation_id"] = cid
    resp = client.post(f"{API}/kunden/eingabe", json=body)
    assert resp.status_code == 200, resp.get_json()
    return resp.get_json()


def _conv(client):
    return client.post(f"{API}/conversations", json={}).get_json()["conversation"]["id"]


# --- Kundenkarte ---------------------------------------------------------------------------


def test_card_shows_next_events_and_booking_for_both_staff(auth_client, employee_client, office, user, employee):
    horna = _c(office, "Thomas Horna")
    first = _ev(office, user, horna, 2, 10)
    _ev(office, user, horna, 5, 11, kind="telefon", location=None)
    for client in (auth_client, employee_client):  # zweiter berechtigter Mitarbeiter sieht dasselbe
        card = client.get(f"{API}/kunden/{horna.id}").get_json()["card"]
        assert [e["id"] for e in card["next_events"]][0] == first.id and len(card["next_events"]) == 2
        assert card["book"]["minutes"] == 30 and {m["id"] for m in card["book"]["members"]} >= {user.id, employee.id}


def test_foreign_office_gets_no_card_booking_or_confirmation(app, client, office, user):
    horna = _c(office, "Thomas Horna")
    event = _ev(office, user, horna, 2, 10)
    other = Tenant(name="Fremd", slug="fremd")
    _db.session.add(other)
    _db.session.commit()
    stranger = User(tenant_id=other.id, email="fremd@example.com", is_active=True, role=UserRole.OFFICE_ADMIN)
    stranger.set_password("testpassword123")
    _db.session.add(stranger)
    _db.session.commit()
    client.post("/auth/login", data={"login_type": "email", "identifier": "fremd@example.com", "password": "testpassword123"})
    assert client.get(f"{API}/kunden/{horna.id}").status_code == 404
    assert client.post(f"{API}/kunden/{horna.id}/termin", json={"day": "2030-01-01", "time": "10:00", "minutes": 30}).status_code == 404
    assert client.post(f"{API}/kunden/{horna.id}/bestaetigung", json={"event_id": event.id}).status_code == 404
    # fremder Termin als "zuletzt gebucht" untergeschoben -> wird nicht verwendet
    r = client.post(f"{API}/kunden/eingabe", json={"text": "Doch um 16:30", "last_event_id": event.id}).get_json()
    assert "Welcher Termin" in r["message"]
    with bypass_tenant_scope():
        assert _db.session.get(CalendarEvent, event.id).version == 1


def test_card_booking_double_tap_and_busy_slot(auth_client, office, user, model_calls):
    horna = _c(office, "Thomas Horna")
    _ev(office, user, None, 2, 16, 30, mins=60, title="Teammeeting")
    day = (local_today() + timedelta(days=2)).isoformat()
    body = {"day": day, "time": "15:00", "minutes": 30, "location_kind": "telefon", "assignee_id": user.id, "client_ref": "ref-book-1"}
    first = auth_client.post(f"{API}/kunden/{horna.id}/termin", json=body)
    second = auth_client.post(f"{API}/kunden/{horna.id}/termin", json=body)  # Doppeltipp
    assert first.status_code == 201 and second.status_code == 200 and second.get_json()["created"] is False
    assert CalendarEvent.query.filter(CalendarEvent.customer_id == horna.id).count() == 1
    assert first.get_json()["card"]["next_events"][0]["id"] == first.get_json()["event"]["id"]
    busy = auth_client.post(f"{API}/kunden/{horna.id}/termin", json={**body, "time": "16:45", "client_ref": "ref-book-2"})
    data = busy.get_json()
    assert busy.status_code == 409 and "Teammeeting" in data["error"] and data["alternatives"]
    assert CalendarEvent.query.filter(CalendarEvent.customer_id == horna.id).count() == 1
    assert model_calls == []


def test_confirmation_from_card_then_shorter_keeps_date_time_place(auth_client, office, user, model_calls):
    horna = _c(office, "Thomas Horna", email="t.horna@example.de")
    event = _ev(office, user, horna, 3, 14, location="Büro Bonn, Hauptstraße 1")
    r = auth_client.post(f"{API}/kunden/{horna.id}/bestaetigung", json={"event_id": event.id}).get_json()
    cid = r["conversation"]["id"]
    assert "Datum:" in r["draft"] and "Ort: Büro Bonn, Hauptstraße 1" in r["draft"]
    short = _say(auth_client, "Kürzer", cid)
    when = to_local(event.starts_at)
    assert short["kind"] == "task" and len(short["draft"]) < len(r["draft"])
    assert f"{when:%d.%m.%Y}, 14:00–14:45 Uhr (Vor Ort, Büro Bonn, Hauptstraße 1)" in short["draft"]
    assert "An: t.horna@example.de" in short["draft"] and "Guten Tag Thomas Horna," in short["draft"]
    # nichts versendet/geaendert, beide Entwuerfe im Verlauf, 0 Modellaufrufe
    assert _db.session.get(CalendarEvent, event.id).version == 1
    assert sum("```E-Mail-Entwurf" in m.content for m in AiMessage.query.filter_by(conversation_id=cid)) == 2
    assert model_calls == []
    # Bestaetigung fuer den Termin eines anderen Kunden ueber diese Karte: abgelehnt
    other = _c(office, "Erika Musterfrau")
    assert auth_client.post(f"{API}/kunden/{other.id}/bestaetigung", json={"event_id": event.id}).status_code == 404


def test_shorter_refers_to_this_chat_only(auth_client, office, user, model_calls):
    horna = _c(office, "Thomas Horna")
    _ev(office, user, horna, 3, 14)
    first = _say(auth_client, "Termin bestätigen", selected_id=horna.id)["conversation"]["id"]
    other = _conv(auth_client)
    r = _say(auth_client, "Kürzer", other)
    assert "Welcher Entwurf" in r["message"] and not r["draft"]
    assert _say(auth_client, "Kürzer", first)["draft"]
    # ohne Chat: Rueckfrage statt "Kein Kunde Kürzer"
    assert "Welcher Entwurf" in _say(auth_client, "Kürzer")["message"]
    assert model_calls == []


def test_shorter_while_waiting_for_customer_is_not_a_name(auth_client, office, user):
    _c(office, "Thomas Horna")
    cid = _say(auth_client, "Termin bestätigen")["conversation"]["id"]
    r = _say(auth_client, "Kürzer", cid)
    assert "Kein Kunde" not in (r.get("message") or "")


# --- "Die Strasse ist falsch" ------------------------------------------------------------------


def test_street_wrong_opens_field_of_selected_customer_without_saving(auth_client, office):
    horna = _c(office, "Thomas Horna", street="Alte Straße", house_number="1")
    r = _say(auth_client, "Die Straße ist falsch", selected_id=horna.id)
    assert r["kind"] == "task" and r["edit"]["field"] == "street" and r["edit"]["card"]["id"] == horna.id
    assert CustomerNote.query.count() == 0  # frueher als Notiz gespeichert
    assert _db.session.get(Customer, horna.id).street == "Alte Straße"
    # erst Speichern aendert die Strasse (vorhandene Bearbeitung mit Versionspruefung)
    card = r["edit"]["card"]
    resp = auth_client.patch(f"{API}/kunden/{horna.id}", json={"version": card["edit"]["version"], "street": "Neue Straße"})
    assert resp.status_code == 200 and _db.session.get(Customer, horna.id).street == "Neue Straße"


def test_street_wrong_without_selection_asks_customer_and_old_card_is_not_used(auth_client, office):
    horna = _c(office, "Thomas Horna")
    _say(auth_client, "Horna")  # Karte erscheint im Verlauf - aber keine sichtbare Auswahl mitgesendet
    r = _say(auth_client, "Die Straße ist falsch")
    assert r["message"] == "Bei welchem Kunden ist die Straße falsch?" and "edit" not in r
    r = _say(auth_client, "Horna", r["conversation"]["id"])
    assert r["edit"]["card"]["id"] == horna.id and r["edit"]["field"] == "street"
    assert CustomerNote.query.count() == 0


def test_phone_wrong_with_two_same_name_customers_asks_selection(auth_client, office):
    a = _c(office, "Max Mustermann", city="Bonn")
    b = _c(office, "Max Mustermann")
    r = _say(auth_client, "Telefonnummer stimmt nicht")
    cid = r["conversation"]["id"]
    r = _say(auth_client, "Mustermann", cid)
    assert {o["choice"]["customer_id"] for o in r["options"]} == {a.id, b.id}
    r = _say(auth_client, "Max Mustermann", cid, task_choice={"customer_id": b.id})
    assert r["edit"]["card"]["id"] == b.id and r["edit"]["field"] == "phone"


# --- "Doch um 16:30" ----------------------------------------------------------------------------


def test_time_change_of_last_booked_event_needs_confirmation(auth_client, office, user, model_calls):
    erika = _c(office, "Erika Musterfrau")
    event = _ev(office, user, erika, 2, 15, mins=30)
    cid = _conv(auth_client)
    r = _say(auth_client, "Doch um 16:00", cid, last_event_id=event.id)
    assert "ändern?" in r["message"] and r["task"]["step"] == "confirm"
    assert _db.session.get(CalendarEvent, event.id).starts_at == event.starts_at  # noch nicht gespeichert
    choice = r["options"][0]["choice"]
    done = _say(auth_client, "Ja", cid, task_choice=choice)
    assert "Termin geändert" in done["message"]
    moved = _db.session.get(CalendarEvent, event.id)
    assert to_local(moved.starts_at).strftime("%H:%M") == "16:00" and (moved.ends_at - moved.starts_at) == timedelta(minutes=30)
    assert moved.version == 2
    # Doppeltipp auf dieselbe Bestaetigung: keine zweite Aenderung, kein Fehler-Durcheinander
    resp = auth_client.post(f"{API}/kunden/eingabe", json={"text": "Ja", "conversation_id": cid, "task_choice": choice})
    assert resp.status_code == 409 and _db.session.get(CalendarEvent, event.id).version == 2
    assert CalendarEvent.query.count() == 1 and model_calls == []


def test_time_change_to_busy_slot_offers_alternatives_and_saves_nothing(auth_client, office, user):
    erika = _c(office, "Erika Musterfrau")
    event = _ev(office, user, erika, 2, 15, mins=30)
    _ev(office, user, None, 2, 16, 30, mins=60, title="Teammeeting")
    cid = _conv(auth_client)
    r = _say(auth_client, "Doch um 16:30", cid, last_event_id=event.id)
    assert "nicht möglich" in r["message"] and "Teammeeting" in r["message"] and r["options"]
    assert _db.session.get(CalendarEvent, event.id).version == 1
    # andere Uhrzeit als Antwort auf dieselbe Rueckfrage
    r = _say(auth_client, "Doch um 17:30", cid)
    assert "ändern?" in r["message"]


def test_time_change_recheck_at_save(auth_client, office, user):
    erika = _c(office, "Erika Musterfrau")
    event = _ev(office, user, erika, 2, 15, mins=30)
    cid = _conv(auth_client)
    r = _say(auth_client, "Doch um 16:30", cid, last_event_id=event.id)
    _ev(office, user, None, 2, 16, 30, mins=30, title="Kurzfristig belegt")  # zwischen Vorschlag und Bestaetigung
    done = _say(auth_client, "Ja", cid, task_choice=r["options"][0]["choice"])
    assert "nicht mehr möglich" in done["message"] and _db.session.get(CalendarEvent, event.id).version == 1


def test_time_change_with_selected_customer_and_several_events_asks_which(auth_client, office, user):
    m = _c(office, "Max Mustermann")
    e1 = _ev(office, user, m, 3, 9)
    _ev(office, user, m, 4, 11)
    cid = _conv(auth_client)
    r = _say(auth_client, "Doch um 10:30", cid, selected_id=m.id)
    assert r["message"].startswith("Welcher Termin von Max Mustermann") and len(r["options"]) == 2
    r = _say(auth_client, "x", cid, task_choice={"event_id": e1.id})
    assert "10:30" in r["message"] and "ändern?" in r["message"]


def test_time_change_without_any_reference_asks(auth_client, office, user):
    r = _say(auth_client, "Doch um 16:30")
    assert r["message"].startswith("Welcher Termin soll auf 16:30 Uhr geändert werden?")
    assert CalendarEvent.query.count() == 0


def test_time_change_with_open_form_only_changes_form(auth_client, office, user):
    erika = _c(office, "Erika Musterfrau")
    event = _ev(office, user, erika, 2, 15)
    r = _say(auth_client, "Doch um 16:30", last_event_id=event.id, form_open=True)
    assert r == {"kind": "form_time", "time": "16:30", "day": None,
                 "message": "Uhrzeit im Terminformular auf 16:30 Uhr gesetzt – noch nicht gespeichert."}
    assert _db.session.get(CalendarEvent, event.id).version == 1 and AiConversation.query.count() == 0


# --- Bestehendes bleibt ---------------------------------------------------------------------------


def test_name_search_and_notes_unchanged(auth_client, office, model_calls):
    horna = _c(office, "Thomas Horna")
    before = AiConversation.query.count()
    r = _say(auth_client, "Horna")
    assert r["kind"] == "card" and r["card"]["id"] == horna.id and AiConversation.query.count() == before
    r = _say(auth_client, "Telefonisch erreicht, schickt Fahrzeugschein", selected_id=horna.id)
    assert r["kind"] == "saved" and CustomerNote.query.filter_by(customer_id=horna.id).count() == 1
    # Notiz ueber "Notiz erfassen" mit derselben client_ref (Doppeltipp) -> eine Notiz
    for _ in range(2):
        auth_client.post(f"{API}/kunden/eingabe", json={"text": "Unterlagen kommen per Post", "customer_id": horna.id, "mode": "note",
                                                        "client_ref": "ref-note-1"})
    assert CustomerNote.query.filter_by(customer_id=horna.id).count() == 2
    # "Später" o. Ae. ohne Uhrzeit ist keine Terminaenderung
    assert _say(auth_client, "Ruft später zurück", selected_id=horna.id)["kind"] == "saved"
    assert model_calls == []
