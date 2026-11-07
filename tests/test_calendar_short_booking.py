"""Kurzer Buchungsauftrag im Eingabefeld ("09.11.26 Schmidthofer Termin um 16.30") - lokal
erkannt und gebucht, ohne KI-Modell. Synthetische Daten."""

from datetime import date, time, timedelta

import pytest

from app.extensions import db as _db
from app.models import CalendarEvent, Customer, EmployeeProfile
from app.models.ai import AiUsage
from app.models.office_settings import OfficeSettings
from app.services import calendar_intent
from app.services.ai import adapters, router
from app.services.timetracking.clock import local_today, to_local

API = "/zentriq/api/kunden/eingabe"
HOURS = {str(d): ["08:00", "18:00"] for d in range(1, 8)}


@pytest.fixture()
def no_ai(monkeypatch):
    """Alle KI-Anbieter aus; jeder Modellaufruf waere ein Testfehler."""

    def forbidden(*args, **kwargs):
        raise AssertionError("Modellaufruf ist hier nicht erlaubt")

    monkeypatch.setattr(adapters, "call", forbidden)
    assert not router.can_answer()


@pytest.fixture()
def office(db, tenant, user, employee):
    db.session.add_all([
        EmployeeProfile(tenant_id=tenant.id, user_id=employee.id, display_name="Laura Test"),
        EmployeeProfile(tenant_id=tenant.id, user_id=user.id, display_name="Anna Admin"),
        OfficeSettings(tenant_id=tenant.id, hours=HOURS, default_minutes=45, buffer_minutes=0, lead_minutes=0, horizon_days=14),
    ])
    schmidthofer = Customer(tenant_id=tenant.id, name="Karl Schmidthofer", name_key="karl schmidthofer", assigned_user_id=employee.id)
    db.session.add(schmidthofer)
    db.session.commit()
    return schmidthofer


def _day():
    return local_today() + timedelta(days=2)


def _cmd(day=None, name="Schmidthofer", clock="16.30", fmt="%d.%m.%y"):
    return f"{(day or _day()).strftime(fmt)} {name} Termin um {clock}"


def _say(client, text, **extra):
    resp = client.post(API, json={"text": text, **extra})
    assert resp.status_code == 200, resp.get_json()
    return resp.get_json()


def test_parse_exact_input_and_variants():
    today = date(2026, 11, 7)
    for text in ("09.11.26 Schmidthofer Termin um 16.30", "09.11.2026 Schmidthofer Termin um 16:30",
                 "09.11.26 Schmidthofer Termin 16:30", "Schmidthofer 09.11.2026 Termin um 16.30 Uhr"):
        p = calendar_intent.parse(text, today)
        assert (p.day, p.start) == (date(2026, 11, 9), time(16, 30)), text
        assert calendar_intent.bare_name(text) == "Schmidthofer", text
    assert calendar_intent.is_booking_command("09.11.26 Schmidthofer Termin um 16.30")


@pytest.mark.parametrize("text", [
    "Ist am 09.11. um 16:30 noch frei?",
    "Hat Laura am 09.11.26 um 16.30 Zeit für einen Termin?",
    "Schreib eine Mail mit Terminvorschlägen",
    "Schreib Schmidthofer eine Mail: Termin am 09.11.26 um 16.30",
    "Termin 09.11.26 um 16.30 für Schmidthofer passt?",
])
def test_questions_and_mail_orders_are_not_booking_commands(text):
    assert not calendar_intent.is_booking_command(text)


def test_exact_command_books_without_ai_and_colleague_sees_it(auth_client, employee_client, office, employee, no_ai):
    data = _say(auth_client, _cmd(), client_ref="s1")
    assert data["kind"] == "calendar_saved", data
    event = CalendarEvent.query.one()
    start = to_local(event.starts_at)
    assert start.date() == _day() and start.strftime("%H:%M") == "16:30"
    assert event.ends_at - event.starts_at == timedelta(minutes=45)  # Buero-Vorgabe
    assert event.customer_id == office.id and event.assignee_user_id == employee.id  # zustaendig laut Kunde
    msg = data["message"]
    assert "Karl Schmidthofer" in msg and _day().strftime("%d.%m.%Y") in msg and "16:30" in msg and "zuständig: Laura Test" in msg
    assert AiUsage.query.count() == 0

    # Zweiter berechtigter Mitarbeiter sieht den Termin im Buerokalender.
    resp = employee_client.get(f"/kalender/api/termine?von={_day().isoformat()}&bis={_day().isoformat()}")
    assert resp.status_code == 200
    seen = [e for e in resp.get_json()["events"] if e["id"] == event.id]
    assert seen and seen[0]["customer"]["name"] == "Karl Schmidthofer" and seen[0]["start"].endswith("16:30")

    # Erneutes Absenden: keine Doppelbuchung.
    again = _say(auth_client, _cmd(), client_ref="s2")
    assert again["kind"] == "calendar_saved" and again["duplicate"] is True
    assert CalendarEvent.query.count() == 1


def test_variants_book_the_same_slot(auth_client, office, no_ai):
    data = _say(auth_client, _cmd(fmt="%d.%m.%Y", clock="16:30"))
    assert data["kind"] == "calendar_saved"
    assert to_local(CalendarEvent.query.one().starts_at).strftime("%H:%M") == "16:30"


def test_busy_slot_shows_alternatives_and_books_chosen_one(auth_client, office, tenant, employee, no_ai):
    other = Customer(tenant_id=tenant.id, name="Eva Beispiel", name_key="eva beispiel", assigned_user_id=employee.id)
    _db.session.add(other)
    _db.session.commit()
    assert _say(auth_client, _cmd(name="Beispiel"))["kind"] == "calendar_saved"

    clash = _say(auth_client, _cmd())
    assert clash["kind"] == "calendar_question" and clash["message"].startswith("Nicht gebucht:")
    assert "Freie Alternativen" in clash["message"] and clash["options"]
    assert CalendarEvent.query.count() == 1
    option = clash["options"][0]
    booked = _say(auth_client, _cmd(), calendar=option["choice"])
    assert booked["kind"] == "calendar_saved", booked
    event = CalendarEvent.query.filter(CalendarEvent.customer_id == office.id).one()
    assert to_local(event.starts_at).strftime("%Y-%m-%dT%H:%M") == option["choice"]["start"]
    assert event.assignee_user_id == employee.id and CalendarEvent.query.count() == 2


def test_ambiguous_name_asks_and_keeps_date_and_time(auth_client, office, tenant, no_ai):
    twin = Customer(tenant_id=tenant.id, name="Maria Schmidthofer", name_key="maria schmidthofer", city="Coburg")
    _db.session.add(twin)
    _db.session.commit()
    data = _say(auth_client, _cmd())
    assert data["kind"] == "calendar_question" and "Mehrere Kunden" in data["message"]
    assert "16:30" in data["message"] and CalendarEvent.query.count() == 0
    labels = [o["label"] for o in data["options"]]
    assert "Karl Schmidthofer" in labels and "Maria Schmidthofer · Coburg" in labels
    # Maria hat keinen Betreuer -> gezielte Rueckfrage nach der Zustaendigkeit, Angaben bleiben.
    pick = next(o for o in data["options"] if o["label"].startswith("Maria"))
    who = _say(auth_client, _cmd(), calendar=pick["choice"])
    assert who["kind"] == "calendar_question" and "zuständiger Mitarbeiter" in who["message"] and "16:30" in who["message"]
    laura = next(o for o in who["options"] if o["label"] == "Laura Test")
    saved = _say(auth_client, _cmd(), calendar=laura["choice"])
    assert saved["kind"] == "calendar_saved" and "Maria Schmidthofer" in saved["message"] and "zuständig: Laura Test" in saved["message"]
    event = CalendarEvent.query.one()
    assert event.customer_id == twin.id and to_local(event.starts_at).strftime("%H:%M") == "16:30"


def test_missing_office_duration_is_asked(auth_client, office, no_ai):
    OfficeSettings.query.delete()
    _db.session.commit()
    data = _say(auth_client, _cmd())
    assert data["kind"] == "calendar_question" and "Wie lange" in data["message"]
    pick = next(o for o in data["options"] if o["label"] == "60 Min.")
    saved = _say(auth_client, _cmd(), calendar=pick["choice"])
    assert saved["kind"] == "calendar_saved"
    event = CalendarEvent.query.one()
    assert event.ends_at - event.starts_at == timedelta(minutes=60)


def test_availability_question_never_books(auth_client, office, no_ai):
    day = _day().strftime("%d.%m.")
    free = _say(auth_client, f"Ist am {day} um 16:30 noch frei?")
    assert free["kind"] == "calendar_list" and "frei" in free["message"] and "nichts gebucht" in free["message"]
    assert CalendarEvent.query.count() == 0
    _say(auth_client, _cmd())  # Laura belegt (Betreuerin von Schmidthofer)
    busy = _say(auth_client, f"Ist am {day} um 16:30 bei Laura noch frei?")
    assert "nicht frei" in busy["message"] and busy["lines"]
    assert CalendarEvent.query.count() == 1
    assert AiUsage.query.count() == 0


def test_mail_with_proposals_goes_to_chat_not_calendar(auth_client, office, no_ai):
    data = _say(auth_client, "Schreib eine Mail mit Terminvorschlägen")
    assert data["kind"] != "calendar_saved"
    assert CalendarEvent.query.count() == 0


def test_alternatives_start_on_requested_day(auth_client, office, tenant, employee, no_ai):
    other = Customer(tenant_id=tenant.id, name="Eva Beispiel", name_key="eva beispiel", assigned_user_id=employee.id)
    _db.session.add(other)
    _db.session.commit()
    assert _say(auth_client, _cmd(name="Beispiel", clock="10:00"))["kind"] == "calendar_saved"
    clash = _say(auth_client, _cmd(clock="10.15"))
    assert clash["kind"] == "calendar_question" and clash["options"]
    first = clash["options"][0]["choice"]["start"]
    assert first[:10] == _day().isoformat() and first[11:] >= "10:45"  # gleicher Tag, nach dem belegten Termin
