# ruff: noqa: F811  (Fixtures werden aus anderen Testmodulen importiert)
"""Eingabefeld: Termine und Notizen getrennt, Rueckfragen bei Widerspruch, Leserecht der Mitarbeiter.

- Ein Terminauftrag legt genau einen Kalendereintrag an und nie eine Kundennotiz - auch nicht bei
  fehlenden Angaben oder fehlgeschlagener Buchung (dann gezielte Rueckfrage).
- Eine Notiz entsteht nur auf ausdruecklichen Wunsch ("Notiere: ...") und erzeugt keinen Termin.
- Nennt die Eingabe einen anderen Kunden als den ausgewaehlten, fragt Zentriq nach.
- Mitarbeiter lesen Kundenakten ihres Bueros (Liste, Akte, Notizen); fremdes Buero -> 404.
Kein KI-Modell (jeder Aufruf waere ein Testfehler). Synthetische Daten."""

from datetime import timedelta

import pytest

from app.extensions import db as _db
from app.models import CalendarEvent, Customer, CustomerMemo, CustomerNote, EmployeeProfile
from app.models.office_settings import OfficeSettings
from app.services.ai import adapters, router
from app.services.timetracking.clock import local_today
from app.tenancy import bypass_tenant_scope
from tests.test_customer_notes import _customer_list, _row
from tests.test_permissions_security import login, world  # noqa: F401

API = "/zentriq/api/kunden/eingabe"
HOURS = {str(d): ["08:00", "18:00"] for d in range(1, 8)}
ORDER = "Mach für diesen Kunden Donnerstag um 15 Uhr einen Termin"


@pytest.fixture()
def no_model(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Modellaufruf ist hier nicht erlaubt")

    monkeypatch.setattr(adapters, "call", forbidden)
    monkeypatch.setattr(router, "complete", forbidden)


@pytest.fixture()
def office(db, tenant, user, employee):
    db.session.add_all([
        EmployeeProfile(tenant_id=tenant.id, user_id=employee.id, display_name="Laura Test"),
        EmployeeProfile(tenant_id=tenant.id, user_id=user.id, display_name="Anna Admin"),
        OfficeSettings(tenant_id=tenant.id, hours=HOURS, default_minutes=45, buffer_minutes=0, lead_minutes=0, horizon_days=21),
    ])
    karl = Customer(tenant_id=tenant.id, name="Karl Schmidthofer", assigned_user_id=employee.id)
    ute = Customer(tenant_id=tenant.id, name="Ute Bergmann", assigned_user_id=employee.id)
    db.session.add_all([karl, ute])
    db.session.commit()
    return {"karl": karl.id, "ute": ute.id}


def _say(client, text, **extra):
    resp = client.post(API, json={"text": text, **extra})
    assert resp.status_code == 200, resp.get_json()
    return resp.get_json()


def _counts():
    return CalendarEvent.query.count(), CustomerNote.query.count()


# --- 1. Termin: genau ein Kalendereintrag, keine Notiz ---------------------------------------------


def test_order_for_selected_customer_creates_one_event_and_no_note(auth_client, office, no_model):
    r = _say(auth_client, ORDER, selected_id=office["karl"], client_ref="t1")
    assert r["kind"] == "calendar_saved", r["message"]
    assert r["event"]["customer"]["id"] == office["karl"]
    # Doppelt abgeschickt (gleiche oder neue Kennung): weiterhin genau ein Termin
    _say(auth_client, ORDER, selected_id=office["karl"], client_ref="t1")
    again = _say(auth_client, ORDER, selected_id=office["karl"], client_ref="t2")
    assert again["kind"] == "calendar_saved" and again["duplicate"] is True
    assert _counts() == (1, 0)


def _next_thursday():
    day = local_today() + timedelta(days=1)
    while day.weekday() != 3:
        day += timedelta(days=1)
    return day.strftime("%d.%m.%Y")


@pytest.mark.parametrize("text,asks", [
    ("Mach für diesen Kunden einen Termin", "fehlt noch das Datum und die Uhrzeit"),
    ("Trage diesen Kunden für den {thu} als Termin im Kalender ein", "fehlt noch die Uhrzeit"),
])
def test_missing_details_are_asked_and_answer_continues_without_note(auth_client, office, no_model, text, asks):
    q = _say(auth_client, text.format(thu=_next_thursday()), selected_id=office["karl"])
    assert q["kind"] == "calendar_question" and asks in q["message"] and q["await_when"]
    assert _counts() == (0, 0)
    answer = "Donnerstag um 11 Uhr" if "Datum" in asks else "um 11 Uhr"
    pending = {"need": "when", **q["await_when"]}
    r = _say(auth_client, answer, selected_id=office["karl"], calendar_pending=pending)
    assert r["kind"] == "calendar_saved" and r["event"]["customer"]["id"] == office["karl"], r["message"]
    assert _counts() == (1, 0)


def test_failed_booking_creates_no_note(auth_client, office, no_model):
    r = _say(auth_client, "Mach für diesen Kunden Sonntag um 15 Uhr einen Termin", selected_id=office["karl"])
    assert r["kind"] == "calendar_question" and r["message"].startswith("Nicht gebucht")
    assert _counts() == (0, 0)
    # belegt: zweiter Termin zur selben Zeit fuer dieselbe Zustaendige
    _say(auth_client, ORDER, selected_id=office["karl"])
    busy = _say(auth_client, "Mach für Ute Bergmann Donnerstag um 15 Uhr einen Termin")
    assert busy["kind"] == "calendar_question" and _counts() == (1, 0)


def test_unclear_input_with_selected_customer_asks_then_books_only(auth_client, office, no_model):
    r = _say(auth_client, "Termin Donnerstag 15 Uhr", selected_id=office["karl"])
    assert r["kind"] == "clarify" and r["options"] == ["calendar", "note"] and _counts() == (0, 0)
    r = _say(auth_client, "Termin Donnerstag 15 Uhr", selected_id=office["karl"], calendar={"book": True})
    assert r["kind"] == "calendar_saved" and r["event"]["customer"]["id"] == office["karl"], r["message"]
    assert _counts() == (1, 0)


# --- 2. Ausdrueckliche Notiz: keine Termine ----------------------------------------------------------


def test_explicit_note_creates_no_event(auth_client, office, no_model):
    r = _say(auth_client, "Notiere: Kunde möchte vor dem Termin am Donnerstag um 15 Uhr angerufen werden", selected_id=office["karl"])
    assert r["kind"] == "saved", r
    note = CustomerNote.query.one()
    assert note.customer_id == office["karl"] and note.text.startswith("Kunde möchte vor dem Termin")
    assert _counts() == (0, 1)


def test_combined_order_does_both_and_note_only_after_booking(auth_client, office, no_model):
    r = _say(auth_client, "Mach Donnerstag um 16 Uhr einen Termin und notiere: Unterlagen mitbringen", selected_id=office["karl"])
    assert r["kind"] == "calendar_saved" and "Notiz bei Karl Schmidthofer gespeichert" in r["message"], r["message"]
    assert _counts() == (1, 1) and CustomerNote.query.one().text == "Unterlagen mitbringen"
    # Buchung scheitert -> keine Notiz; die gewaehlte Alternative bucht und speichert dann die Notiz
    r = _say(auth_client, "Mach Sonntag um 16 Uhr einen Termin und notiere: Rückruf vorher", selected_id=office["karl"])
    assert r["kind"] == "calendar_question" and "Die Notiz wird erst mit dem Termin gespeichert" in r["message"]
    assert _counts() == (1, 1)
    choice = r["options"][0]["choice"]
    assert choice["note"] == "Rückruf vorher"
    r = _say(auth_client, "Mach Sonntag um 16 Uhr einen Termin", selected_id=office["karl"], calendar=choice)
    assert r["kind"] == "calendar_saved", r["message"]
    assert _counts() == (2, 2)


# --- 3. Widerspruechliche Kundenzuordnung -------------------------------------------------------------


def test_other_named_customer_than_selected_asks(auth_client, office, no_model):
    r = _say(auth_client, "Mach für Ute Bergmann Donnerstag um 15 Uhr einen Termin", selected_id=office["karl"])
    assert r["kind"] == "calendar_question" and "ausgewählt ist Karl Schmidthofer" in r["message"]
    assert {o["choice"]["customer_id"] for o in r["options"]} == {office["ute"], office["karl"]}
    r2 = _say(auth_client, "Notiere: Ute Bergmann ruft morgen zurück", selected_id=office["karl"])
    assert r2["kind"] == "conflict" and [c["id"] for c in r2["candidates"]] == [office["ute"], office["karl"]]
    assert _counts() == (0, 0)
    # Auswahl aus der Rueckfrage bucht genau fuer diesen Kunden
    r3 = _say(auth_client, "Mach für Ute Bergmann Donnerstag um 15 Uhr einen Termin", selected_id=office["karl"],
              calendar=next(o["choice"] for o in r["options"] if o["choice"]["customer_id"] == office["ute"]))
    assert r3["kind"] == "calendar_saved" and r3["event"]["customer"]["id"] == office["ute"]
    assert _counts() == (1, 0)


# --- 4. Leserecht der Mitarbeiter ---------------------------------------------------------------------


@pytest.fixture()
def files(app, db, world):
    _doc, ids = _customer_list(db, world.tenant_a.id, [("Max Mustermann", "0228 1234567", [
        _row("808/100001-A", "08/1234-A"), _row("808/100002-B", "08/5555-B")])])
    _doc_b, ids_b = _customer_list(db, world.tenant_b.id, [("Olga Fremd", None, [_row("808/999999-Z", "08/7777-C")])], "b.pdf")
    with bypass_tenant_scope():
        _db.session.add(CustomerMemo(tenant_id=world.tenant_a.id, customer_id=ids["Max Mustermann"], transcript="Gesprächsnotiz zum Kfz-Wechsel",
                                     transcript_sha256="0" * 64, matched_by="selected", created_by_user_id=world.laura.id))
        _db.session.commit()
    world.max, world.olga = ids["Max Mustermann"], ids_b["Olga Fremd"]
    return world


def test_employee_reads_customer_file_of_own_office(app, files):
    laura = login(app, "laura@example.com")
    laura.post(API, json={"text": "Notiz zum Lesen", "customer_id": files.max, "mode": "note"})
    page = login(app, "dennis@example.com").get(f"/customers/{files.max}")
    html = page.get_data(as_text=True)
    assert page.status_code == 200
    assert "Max Mustermann" in html and "Notiz zum Lesen" in html and "Gesprächsnotiz zum Kfz-Wechsel" in html
    # Leipziger Liste wie in der Liste: nur die eigene Vermittlernummer (Dennis 08/1234-A)
    assert "808/100001-A" in html and "808/100002-B" not in html
    assert "/documents/" not in html and "/dublette/" not in html  # Verwaltung bleibt Admins vorbehalten
    assert login(app, "dennis@example.com").get("/customers?q=Mustermann").status_code == 200
    # Assistent: Kundenkarte verlinkt die Akte
    card = login(app, "dennis@example.com").get(f"/zentriq/api/kunden/{files.max}").get_json()["card"]
    assert card["customer_url"] == f"/customers/{files.max}"


def test_foreign_office_file_is_rejected(app, files):
    dennis = login(app, "dennis@example.com")
    assert dennis.get(f"/customers/{files.olga}").status_code == 404
    assert dennis.get(f"/customers/{files.olga}?panel=1").status_code == 404
    assert dennis.get(f"/zentriq/api/kunden/{files.olga}").status_code == 404
    assert login(app, "bob@example.com").get(f"/customers/{files.max}").status_code == 404


def test_read_access_does_not_grant_edit_or_admin_actions(app, files):
    dennis = login(app, "dennis@example.com")
    card = dennis.get(f"/zentriq/api/kunden/{files.max}").get_json()["card"]
    assert card["can_edit"] is False
    assert dennis.patch(f"/zentriq/api/kunden/{files.max}", json={"version": "", "email": "a@b.de"}).status_code == 403
    assert dennis.get(f"/customers/{files.max}/dublette/{files.max}").status_code == 403
    assert dennis.post(f"/customers/{files.max}/zusammenfuehren/{files.max}", data={"bestaetigt": "ja"}).status_code == 403
    assert login(app, "justin@example.com").get(f"/customers/{files.max}").status_code == 403
