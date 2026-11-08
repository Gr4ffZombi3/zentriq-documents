"""Offener Auftrag im Hauptchat: "Termin bestaetigen" -> "Fuer welchen Kunden?" -> nur der Name ->
fertiger Entwurf. Alles im Backend mit Textvorlage und 0 Modellaufrufen (router.complete ist so
ersetzt, dass jeder Aufruf gezaehlt wird). Nichts wird gesendet, am Kalender aendert sich nichts."""

from datetime import time, timedelta

import pytest

from app.extensions import db as _db
from app.models import CalendarEvent, Customer, CustomerNote, OfficeSettings
from app.models.ai import AiConversation, AiMessage
from app.services import chat_task
from app.services.ai import router
from app.services.timetracking.clock import local_to_utc_naive, local_today

API = "/zentriq/api"
CONFIRM = "Schreib eine E-Mail, um den Termin zu bestätigen"


@pytest.fixture()
def model_calls(monkeypatch):
    calls = []

    class Fake:
        text = "gern bestätige ich unseren Termin.\n\n[[TERMINDATEN]]\n\nIch freue mich auf Sie!"
        provider, model, model_label, tools_used = "fake", "fake", "Fake", []
        input_tokens = output_tokens = 0
        cost_usd, cached, fallback_used = 0, False, False

    def complete(**kwargs):
        calls.append(kwargs)
        return Fake()

    monkeypatch.setattr(router, "complete", complete)
    monkeypatch.setattr(router, "can_answer", lambda: True)
    return calls


@pytest.fixture()
def office(app, tenant, user):
    _db.session.add(OfficeSettings(tenant_id=tenant.id, office_name="Büro Test", email="info@buero-test.de", phone="0228 123450"))
    _db.session.commit()
    return tenant


def _customer(tenant, name, **kw):
    c = Customer(tenant_id=tenant.id, name=name, **kw)
    _db.session.add(c)
    _db.session.commit()
    return c


def _event(tenant, user, customer, days, hour, title="Beratung Kfz", kind="vor_ort", location="Büro Bonn, Hauptstraße 1"):
    day = local_today() + timedelta(days=days)
    start = local_to_utc_naive(day, time(hour, 0))
    e = CalendarEvent(tenant_id=tenant.id, title=title, starts_at=start, ends_at=start + timedelta(minutes=45),
                      assignee_user_id=user.id, customer_id=customer.id if customer else None, location_kind=kind, location=location)
    _db.session.add(e)
    _db.session.commit()
    return e


def _say(client, text, conversation_id=None, **extra):
    body = {"text": text, **extra}
    if conversation_id:
        body["conversation_id"] = conversation_id
    resp = client.post(f"{API}/kunden/eingabe", json=body)
    assert resp.status_code == 200, resp.get_json()
    return resp.get_json()


def test_detection_of_confirmation_orders():
    for text in (CONFIRM, "Termin bestätigen", "Bestätige den Termin mit Horna per Mail", "Terminbestätigung an Horna",
                 "Schreib Horna eine Mail zur Bestätigung des Termins"):
        assert chat_task.is_confirm_request(text), text
    for text in ("Ist der Termin bestätigt?", "Termin bestätigt, kommt um 14 Uhr", "Horna", "Termin am 12.11. um 14 Uhr eintragen"):
        assert not chat_task.is_confirm_request(text), text


def test_flow_ask_customer_then_name_only_gives_draft(auth_client, office, user, model_calls):
    horna = _customer(office, "Thomas Horna", email="t.horna@example.de", customer_number="808/654321-E")
    event = _event(office, user, horna, 3, 14)
    events_before = CalendarEvent.query.count()

    r = _say(auth_client, CONFIRM)
    assert r["kind"] == "task" and r["message"] == "Für welchen Kunden?" and not r["draft"]
    assert r["task"]["label"].startswith("Kunde für die Terminbestätigung")
    cid = r["conversation"]["id"]  # Chat wird fuer den offenen Auftrag angelegt

    r = _say(auth_client, "Horna", cid)
    assert r["kind"] == "task" and r["task"] is None
    draft = r["draft"]
    when = local_today() + timedelta(days=3)
    assert "An: t.horna@example.de" in draft and "Guten Tag Thomas Horna," in draft
    assert f"Datum: {chat_task.WEEKDAY_NAMES[when.weekday()]}, {when:%d.%m.%Y}" in draft
    assert "Uhrzeit: 14:00 – 14:45 Uhr" in draft and "Terminart: Vor Ort" in draft and "Ort: Büro Bonn, Hauptstraße 1" in draft
    assert "Büro Test" in draft  # Signatur aus dem Buero-Profil
    assert "nicht gesendet" in r["message"]
    # Verlauf bleibt nach dem Neuladen erhalten; nichts gebucht/geaendert; kein Modell
    conv = auth_client.get(f"{API}/conversations/{cid}").get_json()
    contents = [m["content"] for m in conv["messages"]]
    assert contents[0] == CONFIRM and "Für welchen Kunden?" in contents[1] and contents[2] == "Horna" and "```E-Mail-Entwurf" in contents[3]
    assert conv["conversation"]["task"] is None
    assert CalendarEvent.query.count() == events_before and _db.session.get(CalendarEvent, event.id).version == 1
    assert model_calls == []


def test_vn_number_as_answer(auth_client, office, user, model_calls):
    horna = _customer(office, "Thomas Horna", customer_number="808/654321-E")
    _event(office, user, horna, 2, 10)
    cid = _say(auth_client, "Termin bestätigen")["conversation"]["id"]
    r = _say(auth_client, "808/654321-E", cid)
    assert r["draft"] and "Thomas Horna" in r["draft"] and model_calls == []


def test_selected_customer_is_used_without_question(auth_client, office, user, model_calls):
    horna = _customer(office, "Thomas Horna")
    _customer(office, "Erika Musterfrau")
    _event(office, user, horna, 1, 9, kind="telefon", location=None)
    r = _say(auth_client, CONFIRM, selected_id=horna.id)
    assert r["kind"] == "task" and r["draft"] and "Terminart: Telefon" in r["draft"] and "Ort:" not in r["draft"]
    # ausdruecklich anderer Kunde genannt -> dieser, nicht der ausgewaehlte
    erika = Customer.query.filter_by(name="Erika Musterfrau").one()
    _event(office, user, erika, 4, 11)
    r = _say(auth_client, "Bestätige den Termin mit Erika Musterfrau per Mail", selected_id=horna.id)
    assert "Erika Musterfrau" in r["draft"] and model_calls == []


def test_two_customers_with_same_name_are_distinguishable_and_flow_continues(auth_client, office, user, model_calls):
    a = _customer(office, "Max Mustermann", city="Bonn", postal_code="53111")
    b = _customer(office, "Max Mustermann")
    _db.session.add(CustomerNote(tenant_id=office.id, customer_id=a.id, text="Rückruf wegen Kfz", created_by_user_id=user.id))
    _db.session.commit()
    _event(office, user, b, 5, 15)
    cid = _say(auth_client, CONFIRM)["conversation"]["id"]
    r = _say(auth_client, "Mustermann", cid)
    assert r["kind"] == "task" and len(r["options"]) == 2 and r["task"]["step"] == "choose_customer"
    subs = {o["choice"]["customer_id"]: o["sub"] for o in r["options"]}
    assert f"Akte {a.id}" in subs[a.id] and "1 Notiz ·" in subs[a.id] and "53111 Bonn" in subs[a.id]
    assert f"Akte {b.id}" in subs[b.id] and "0 Notizen" in subs[b.id] and "1 bevorstehender Termin" in subs[b.id]
    assert Customer.query.filter_by(name="Max Mustermann").count() == 2  # nicht zusammengefuehrt
    r = _say(auth_client, "Max Mustermann", cid, task_choice={"customer_id": b.id}, choice_label="Max Mustermann")
    assert r["draft"] and r["task"] is None and model_calls == []


def test_several_appointments_ask_which_one(auth_client, office, user, model_calls):
    horna = _customer(office, "Thomas Horna")
    first = _event(office, user, horna, 2, 9)
    second = _event(office, user, horna, 6, 16, kind="video", location=None)
    cid = _say(auth_client, CONFIRM)["conversation"]["id"]
    r = _say(auth_client, "Horna", cid)
    assert r["message"].startswith("Welchen Termin möchtest du bestätigen?")
    assert [o["choice"]["event_id"] for o in r["options"]] == [first.id, second.id]
    assert r["task"]["label"].endswith("(Thomas Horna)")
    r = _say(auth_client, "2", cid)  # getippte Auswahl
    assert "Terminart: Video" in r["draft"] and "16:00" in r["draft"]
    # erneut, diesmal per Klick
    cid = _say(auth_client, CONFIRM, selected_id=horna.id)["conversation"]["id"]
    r = _say(auth_client, "x", cid, task_choice={"event_id": first.id})
    assert "09:00" in r["draft"] and model_calls == []


def test_no_appointment_asks_instead_of_inventing(auth_client, office, user, model_calls):
    horna = _customer(office, "Thomas Horna")
    _event(office, user, None, 2, 9, title="Teammeeting")  # fremder Termin zaehlt nicht
    cid = _say(auth_client, CONFIRM)["conversation"]["id"]
    r = _say(auth_client, "Thomas Horna", cid)
    assert "kein bevorstehender Termin" in r["message"] and not r["draft"] and r["task"]["step"] == "event_text"
    r = _say(auth_client, "12.11. um 14 Uhr", cid)
    assert "Uhrzeit: 14:00 Uhr" in r["draft"] and "Terminart" not in r["draft"] and "nicht im Kalender gefunden" in " ".join(r["status"])
    assert CalendarEvent.query.filter(CalendarEvent.customer_id == horna.id).count() == 0
    assert model_calls == []


def test_name_without_open_task_stays_direct_search(auth_client, office, model_calls):
    _customer(office, "Thomas Horna")
    before = AiConversation.query.count()
    r = _say(auth_client, "Horna")
    assert r["kind"] == "card" and r["card"]["name"] == "Thomas Horna"
    assert AiConversation.query.count() == before and AiMessage.query.count() == 0 and model_calls == []
    # auch in einem Chat ohne offenen Auftrag
    cid = auth_client.post(f"{API}/conversations", json={}).get_json()["conversation"]["id"]
    r = _say(auth_client, "Horna", cid)
    assert r["kind"] == "card" and "task_ended" not in r


def test_new_order_ends_open_task_and_cancel(auth_client, office, user, model_calls):
    horna = _customer(office, "Thomas Horna")
    _event(office, user, horna, 2, 9)
    cid = _say(auth_client, CONFIRM)["conversation"]["id"]
    r = _say(auth_client, "Wie viele Kunden haben offene Rückrufe?", cid)
    assert r["kind"] != "task" and r.get("task_ended") is True
    assert _db.session.get(AiConversation, cid).pending_task is None
    # neuer Auftrag ersetzt den alten
    _say(auth_client, CONFIRM, cid)
    r = _say(auth_client, "Termin bestätigen", cid, selected_id=horna.id)
    assert r["draft"]
    # Abbrechen per Eingabe und per Knopf
    _say(auth_client, CONFIRM, cid)
    r = _say(auth_client, "Abbrechen", cid)
    assert r["message"].startswith("Abgebrochen") and r["task"] is None
    _say(auth_client, CONFIRM, cid)
    resp = auth_client.post(f"{API}/conversations/{cid}/auftrag/abbrechen")
    assert resp.get_json()["cancelled"] is True and resp.get_json()["conversation"]["task"] is None
    # danach ist ein Name wieder die direkte Suche
    assert _say(auth_client, "Horna", cid)["kind"] == "card"
    assert model_calls == []


def test_stale_choice_is_rejected(auth_client, office, user):
    a = _customer(office, "Max Mustermann")
    _customer(office, "Max Mustermann")
    cid = _say(auth_client, CONFIRM)["conversation"]["id"]
    resp = auth_client.post(f"{API}/kunden/eingabe", json={"text": "x", "conversation_id": cid, "task_choice": {"customer_id": a.id}})
    assert resp.status_code == 409  # Auftrag wartet auf Namen, nicht auf eine Auswahl


def test_other_users_conversation_is_not_used(app, auth_client, employee_client, office, user):
    _customer(office, "Thomas Horna")
    cid = _say(auth_client, CONFIRM)["conversation"]["id"]
    r = employee_client.post(f"{API}/kunden/eingabe", json={"text": "Horna", "conversation_id": cid}).get_json()
    assert r["kind"] == "card"  # fremder Chat: kein Zugriff auf dessen offenen Auftrag
    assert _db.session.get(AiConversation, cid).pending_task is not None


def test_tone_wish_uses_model_without_customer_data(auth_client, office, user, model_calls):
    horna = _customer(office, "Thomas Horna", email="t.horna@example.de", phone="0171 2223334")
    _event(office, user, horna, 2, 9)
    r = _say(auth_client, "Schreib Thomas Horna eine lockere Mail, um den Termin zu bestätigen")
    assert r["used_model"] is True and len(model_calls) == 1
    sent = str(model_calls[0]["messages"]) + model_calls[0]["system"]
    for secret in ("Horna", "Thomas", "t.horna@example.de", "0171", "Hauptstraße"):
        assert secret not in sent, secret
    assert "Ich freue mich auf Sie!" in r["draft"] and "Ort: Büro Bonn, Hauptstraße 1" in r["draft"]


def test_worker_path_handles_confirmation_without_model(auth_client, office, user, model_calls):
    horna = _customer(office, "Thomas Horna")
    _event(office, user, horna, 2, 9)
    cid = auth_client.post(f"{API}/conversations", json={}).get_json()["conversation"]["id"]
    resp = auth_client.post(f"{API}/conversations/{cid}/messages", json={"content": "Termin bestätigen"})
    reply = AiMessage.query.filter(AiMessage.id == resp.get_json()["reply"]["id"]).first()
    assert reply.content == "Für welchen Kunden?"
    resp = auth_client.post(f"{API}/conversations/{cid}/messages", json={"content": "Horna"})
    reply = AiMessage.query.filter(AiMessage.id == resp.get_json()["reply"]["id"]).first()
    assert "```E-Mail-Entwurf" in reply.content and model_calls == []


def test_card_notes_and_calendar_use_same_customer_id(auth_client, office, user):
    """Karte mit Notizen vs. "Keine Notizen vorhanden": zwei Akten gleichen Namens. Die Karte
    zeigt die Notizen der Akte mit genau dieser ID; Kalender und Entwurf verwenden dieselbe ID."""
    a = _customer(office, "Max Mustermann")
    b = _customer(office, "Max Mustermann")
    _db.session.add(CustomerNote(tenant_id=office.id, customer_id=a.id, text="Rückruf wegen Kfz", created_by_user_id=user.id))
    _db.session.commit()
    card_a = auth_client.get(f"{API}/kunden/{a.id}").get_json()["card"]
    card_b = auth_client.get(f"{API}/kunden/{b.id}").get_json()["card"]
    assert card_a["id"] == a.id and card_a["latest"]["text"] == "Rückruf wegen Kfz"
    assert card_b["id"] == b.id and card_b["latest"] is None  # -> "Keine Notizen vorhanden"
    event = _event(office, user, b, 2, 9)
    assert [e.id for e in chat_task.upcoming_events(a)] == [] and [e.id for e in chat_task.upcoming_events(b)] == [event.id]
    # Namenssuche zeigt beide Akten zur Auswahl, unterscheidbar
    r = _say(auth_client, "Max Mustermann")
    assert r["kind"] == "choose" and {c["id"] for c in r["candidates"]} == {a.id, b.id}
    assert {c["notes"] for c in r["candidates"]} == {1, 0}
