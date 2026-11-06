"""Termine ueber das Eingabefeld des Assistenten."""

from datetime import timedelta

import pytest

from app.models import CalendarEvent, Customer, EmployeeProfile
from app.services.calendar_intent import _next_weekday, parse
from app.services.timetracking.clock import local_today, to_local

API = "/zentriq/api/kunden/eingabe"


@pytest.fixture()
def office(db, tenant, user, employee):
    db.session.add_all([
        EmployeeProfile(tenant_id=tenant.id, user_id=employee.id, display_name="Laura Test"),
        EmployeeProfile(tenant_id=tenant.id, user_id=user.id, display_name="Anna Admin"),
        Customer(tenant_id=tenant.id, name="Max Mustermann", name_key="max mustermann"),
        Customer(tenant_id=tenant.id, name="Erika Muster", name_key="erika muster"),
        Customer(tenant_id=tenant.id, name="Erika Muster", name_key="erika muster", city="Coburg"),
    ])
    db.session.commit()


def _say(client, text, **extra):
    resp = client.post(API, json={"text": text, **extra})
    assert resp.status_code == 200, resp.get_json()
    return resp.get_json()


def test_example_from_request_is_saved(auth_client, office, employee):
    data = _say(auth_client, "Termin mit Max Mustermann Donnerstag um 14 Uhr, 30 Minuten, bei Laura.", client_ref="t1")
    assert data["kind"] == "calendar_saved", data
    event = CalendarEvent.query.one()
    start = to_local(event.starts_at)
    expected_day = _next_weekday(local_today(), 3, False)
    assert start.date() == expected_day and start.strftime("%H:%M") == "14:00"
    assert (event.ends_at - event.starts_at) == timedelta(minutes=30)
    assert event.assignee_user_id == employee.id and event.customer.name == "Max Mustermann"
    assert "Termin gespeichert" in data["message"] and "Laura Test" in data["message"]
    # Erneutes Absenden derselben Eingabe: kein zweiter Termin.
    again = _say(auth_client, "Termin mit Max Mustermann Donnerstag um 14 Uhr, 30 Minuten, bei Laura.", client_ref="t1")
    assert again["duplicate"] is True and CalendarEvent.query.count() == 1


def test_unknown_person_asks_before_saving(auth_client, office, employee):
    data = _say(auth_client, "Termin mit Max Mustermann morgen um 10 Uhr bei Kevin")
    assert data["kind"] == "calendar_question" and "Kevin" in data["message"]
    assert CalendarEvent.query.count() == 0
    choice = next(o["choice"] for o in data["options"] if o["label"] == "Laura Test")
    saved = _say(auth_client, "Termin mit Max Mustermann morgen um 10 Uhr bei Kevin", calendar=choice)
    assert saved["kind"] == "calendar_saved" and CalendarEvent.query.one().assignee_user_id == employee.id


def test_ambiguous_customer_and_missing_time(auth_client, office):
    data = _say(auth_client, "Termin mit Erika Muster übermorgen um 9 Uhr")
    assert data["kind"] == "calendar_question" and len(data["options"]) == 3  # 2 Kunden + ohne Verknuepfung
    missing = _say(auth_client, "Termin mit Max Mustermann übermorgen")
    assert missing["kind"] == "calendar_question" and "Uhrzeit" in missing["message"]
    assert "/kalender?" in missing["form_url"]
    assert CalendarEvent.query.count() == 0


def test_unknown_customer_can_be_saved_without_link(auth_client, office):
    data = _say(auth_client, "Termin mit Hans Unbekannt morgen um 11:30, eine Stunde")
    assert data["kind"] == "calendar_question"
    choice = data["options"][0]["choice"]
    saved = _say(auth_client, "Termin mit Hans Unbekannt morgen um 11:30, eine Stunde", calendar=choice)
    event = CalendarEvent.query.one()
    assert saved["kind"] == "calendar_saved" and event.customer_id is None and event.title == "Termin mit Hans Unbekannt"
    assert (event.ends_at - event.starts_at) == timedelta(hours=1)


def test_list_question(auth_client, office):
    _say(auth_client, "Termin mit Max Mustermann morgen um 10 Uhr")
    data = _say(auth_client, "Welche Termine habe ich morgen?")
    assert data["kind"] == "calendar_list" and len(data["lines"]) == 1


def test_notes_still_work(auth_client, office):
    # Kein Termin-Wunsch: normale Kundennotiz-/Suchlogik bleibt unveraendert.
    data = _say(auth_client, "Max Mustermann Hausrat Angebot, bitte zurückrufen")
    assert data["kind"] != "calendar_saved"


@pytest.mark.parametrize(
    "text, hh, minutes",
    [
        ("Termin morgen 9:15 Uhr 45 Minuten", "09:15", 45),
        ("Termin morgen von 13 bis 14:30 Uhr", "13:00", 90),
        ("Termin morgen um 8 Uhr, 1,5 Stunden", "08:00", 90),
    ],
)
def test_parser_times(text, hh, minutes):
    p = parse(text, local_today())
    assert p.start.strftime("%H:%M") == hh
    total = p.minutes if p.end is None else (p.end.hour * 60 + p.end.minute) - (p.start.hour * 60 + p.start.minute)
    assert total == minutes
