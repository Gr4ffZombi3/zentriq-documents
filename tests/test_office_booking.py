# ruff: noqa: F811  (Fixtures werden aus anderen Testmodulen importiert)
"""Allgemeiner Buchungslink je Buero (/termin/<adresse>). Fiktive Daten.

Buero A: Admin A, Laura (Mitarbeiterin), Dennis. Online-Fenster: Mo/Di/Do/Fr 09:00-12:00 (+ Do 14-16),
Mittwoch ohne Freigabe; Kalender von Laura; Dauer 45 Minuten."""

import re
from datetime import datetime, time, timedelta

import pytest

from app.extensions import db as _db
from app.models import BookingLink, CalendarEvent, OfficeBookingPage, User
from app.models.office_settings import OfficeSettings
from app.services import office_booking as ob
from app.services.timetracking.clock import local_to_utc_naive, local_today
from app.tenancy import bypass_tenant_scope
from tests.test_booking_links import _create, _csrf
from tests.test_customer_notes import office  # noqa: F401
from tests.test_permissions_security import login, world  # noqa: F401

WINDOWS = {"ob_von_1_1": "09:00", "ob_bis_1_1": "12:00", "ob_von_2_1": "09:00", "ob_bis_2_1": "12:00",
           "ob_von_4_1": "09:00", "ob_bis_4_1": "12:00", "ob_von_4_2": "14:00", "ob_bis_4_2": "16:00",
           "ob_von_5_1": "09:00", "ob_bis_5_1": "12:00"}


@pytest.fixture()
def setup(app, office):
    with bypass_tenant_scope():
        _db.session.add(OfficeSettings(tenant_id=office.tenant_a.id, hours={str(d): ["08:00", "18:00"] for d in range(1, 8)},
                                       default_minutes=30, buffer_minutes=0, lead_minutes=0, office_name="Agentur Heller"))
        laura = User.query.filter_by(email="laura@example.com").one()
        laura.can_send_booking_links = True
        _db.session.commit()
    return office


def _enable(app, setup, **extra):
    admin = login(app, "admin-a@example.com")
    data = {"enabled": "1", "slug": "heller", "title": "Beratungstermin", "minutes": "45", "assignee_id": str(setup.laura_id),
            "horizon_days": "14", **WINDOWS, **extra}
    r = admin.post("/kalender/einstellungen/online-buchung", data=data)
    assert r.status_code == 302, r.get_data(as_text=True)[:400]
    return admin


def _next(weekday):  # ISO 1..7, mindestens morgen
    day = local_today() + timedelta(days=1)
    while day.isoweekday() != weekday:
        day += timedelta(days=1)
    return day


def _book(client, value, name="Kunde Eins", submission=None, page=None):
    page = page or client.get("/termin/heller").get_data(as_text=True)
    return client.post("/termin/heller", data={"slot": value, "name": name, "phone": "0171 1234567",
                                                "submission": submission or _csrf(page)})


def _event(day, start, end, title, assignee):
    return CalendarEvent(tenant_id=assignee.tenant_id, title=title, assignee_user_id=assignee.id, created_by_user_id=assignee.id,
                         starts_at=local_to_utc_naive(day, start), ends_at=local_to_utc_naive(day, end), status="scheduled")


def test_settings_only_for_office_admin_and_unique_address(app, setup):
    assert login(app, "laura@example.com").post("/kalender/einstellungen/online-buchung", data={"slug": "x"}).status_code == 403
    _enable(app, setup)
    with bypass_tenant_scope():
        page = OfficeBookingPage.query.one()
        assert page.slug == "heller" and page.enabled and page.minutes == 45 and page.windows["4"] == [["09:00", "12:00"], ["14:00", "16:00"]]
    # gleiche Adresse in einem anderen Buero: abgelehnt
    other = login(app, "admin-b@example.com").post("/kalender/einstellungen/online-buchung",
                                                   data={"enabled": "1", "slug": "heller", "minutes": "30", "assignee_id": "0", **WINDOWS})
    assert other.status_code == 422


def test_laura_copies_general_link_and_proposals_stay(app, setup):
    _enable(app, setup)
    html = login(app, "laura@example.com").get("/kalender/buchungslinks").get_data(as_text=True)
    assert "Termine vorschlagen" in html and "Allgemeinen Buchungslink kopieren" in html
    assert "https://zentriq.test/termin/heller" in html and "data-bl-office-copy" in html
    assert login(app, "dennis@example.com").get("/kalender/buchungslinks").status_code == 403  # ohne Freigabe


def test_disabled_link_offers_nothing(app, setup):
    _enable(app, setup)
    _enable(app, setup, enabled="")  # Admin deaktiviert
    page = app.test_client().get("/termin/heller")
    assert page.status_code == 200 and "nicht verfügbar" in page.get_data(as_text=True)
    assert "Allgemeinen Buchungslink kopieren" in login(app, "laura@example.com").get("/kalender/buchungslinks").get_data(as_text=True)
    assert "data-bl-office-copy" not in login(app, "laura@example.com").get("/kalender/buchungslinks").get_data(as_text=True)


def test_public_page_only_free_times_whole_duration_and_wednesday_empty(app, setup):
    _enable(app, setup)
    thursday, wednesday = _next(4), _next(3)
    with bypass_tenant_scope():
        laura = User.query.filter_by(email="laura@example.com").one()
        _db.session.add(_event(thursday, time(10, 0), time(10, 30), "Interner Geheimtermin", laura))
        _db.session.add(_event(thursday, time(14, 0), time(16, 0), "Abwesenheit Schule", laura))  # Sperrzeit
        _db.session.commit()
    client = app.test_client()  # ohne Anmeldung
    html = client.get("/termin/heller").get_data(as_text=True)
    assert "Agentur Heller" in html and "Beratungstermin" in html and "45 Minuten" in html
    assert "Geheimtermin" not in html and "Schule" not in html and "laura" not in html.lower()
    slots = set(re.findall(r'name="slot" value="([^"]+)"', html))
    th = thursday.isoformat()
    assert f"{th}T09:00" in slots and f"{th}T11:00" in slots
    assert f"{th}T09:30" not in slots and f"{th}T10:00" not in slots  # ueberschneidet 10:00-10:30
    assert f"{th}T11:30" not in slots  # 11:30 + 45 Min. passt nicht mehr ins Fenster bis 12:00
    assert not any(s.startswith(f"{th}T14") or s.startswith(f"{th}T15") for s in slots)  # Abwesenheit
    assert not any(s.startswith(wednesday.isoformat()) for s in slots)  # Mittwoch ohne Freigabe
    wed_label = f"Mittwoch, {wednesday.strftime('%d.%m.%Y')}"
    assert wed_label in html and "Keine buchbaren Zeiten" in html
    today = local_today().isoformat()
    now_local = datetime.now().strftime("%H:%M")
    assert not any(s.startswith(today) and s[11:] <= now_local for s in slots)  # nur Zukunft


def test_booking_lands_in_calendar_link_stays_valid_and_double_click_is_one(app, setup):
    _enable(app, setup)
    monday = _next(1)
    client = app.test_client()
    page = client.get("/termin/heller").get_data(as_text=True)
    submission = _csrf(page)
    first = _book(client, f"{monday.isoformat()}T09:00", submission=submission, page=page)
    assert first.status_code == 303
    again = _book(client, f"{monday.isoformat()}T09:00", submission=submission, page=page)  # Doppelklick
    assert again.status_code == 303
    confirmed = client.get(first.headers["Location"]).get_data(as_text=True)
    assert "Ihr Termin ist gebucht" in confirmed and monday.strftime("%d.%m.%Y") in confirmed and "09:00" in confirmed
    with bypass_tenant_scope():
        events = CalendarEvent.query.filter(CalendarEvent.client_ref.like("ob-%")).all()
        assert len(events) == 1 and events[0].assignee_user_id == setup.laura_id and "Kunde Eins" in events[0].title
        assert "0171 1234567" in (events[0].note or "")
    # derselbe Link fuer den naechsten Kunden
    other = app.test_client()
    html = other.get("/termin/heller").get_data(as_text=True)
    assert f"{monday.isoformat()}T09:00" not in html  # vergeben
    assert _book(other, f"{monday.isoformat()}T10:00", name="Kunde Zwei", page=html).status_code == 303
    with bypass_tenant_scope():
        assert CalendarEvent.query.filter(CalendarEvent.client_ref.like("ob-%")).count() == 2


def test_taken_time_gives_notice_and_updated_choice(app, setup):
    _enable(app, setup)
    tuesday = _next(2)
    a, b = app.test_client(), app.test_client()
    page_a, page_b = a.get("/termin/heller").get_data(as_text=True), b.get("/termin/heller").get_data(as_text=True)
    value = f"{tuesday.isoformat()}T09:00"
    assert _book(a, value, name="Kunde A", page=page_a).status_code == 303
    late = _book(b, value, name="Kunde B", page=page_b)
    html = late.get_data(as_text=True)
    assert late.status_code == 409 and "nicht mehr frei" in html
    assert f'value="{value}"' not in html  # aktualisierte Auswahl ohne die vergebene Zeit
    assert 'value="Kunde B"' in html  # Eingaben bleiben erhalten
    with bypass_tenant_scope():
        assert CalendarEvent.query.filter(CalendarEvent.client_ref.like("ob-%")).count() == 1


def test_internal_calendar_and_proposal_links_respect_each_other(app, setup):
    _enable(app, setup)
    friday = _next(5)
    # persoenlicher Vorschlagslink (bestehende Funktion) bucht 10:00 bei Laura ...
    admin, token, _day = _create(app, setup, slots=("10:00",), customer=False)
    with bypass_tenant_scope():
        link = BookingLink.query.one()
        link.offers = [f"{friday.isoformat()}T10:00"]
        _db.session.commit()
    c = app.test_client()
    page = c.get(f"/termin/buchen/{token}").get_data(as_text=True)
    r = c.post(f"/termin/buchen/{token}", data={"slot": f"{friday.isoformat()}T10:00", "name": "Vorschlag Kunde", "phone": "0171 2223334",
                                                "submission": _csrf(page)})
    assert r.status_code == 303
    # ... danach bietet der allgemeine Link 10:00 (und 09:30, ueberschneidend) nicht mehr an
    html = app.test_client().get("/termin/heller").get_data(as_text=True)
    assert f"{friday.isoformat()}T10:00" not in html and f"{friday.isoformat()}T09:30" not in html
    assert f"{friday.isoformat()}T09:00" in html  # 09:00-09:45 endet vor 10:00 (kein Puffer)
    # ... und ein Vorschlagslink kann die vom allgemeinen Link gebuchte Zeit nicht mehr vergeben
    c2 = app.test_client()
    page_ob = c2.get("/termin/heller").get_data(as_text=True)
    assert _book(c2, f"{friday.isoformat()}T09:00", name="Online Kunde", page=page_ob).status_code == 303
    with bypass_tenant_scope():
        laura = User.query.get(setup.laura_id)
        start = local_to_utc_naive(friday, time(9, 0))
        assert not ob.is_free(OfficeBookingPage.query.one(), start)
        assert __import__("app.services.availability", fromlist=["check"]).check(laura, start, 30) is not None


def test_unknown_address_and_old_token_links_still_work(app, setup):
    assert app.test_client().get("/termin/gibt-es-nicht").status_code == 404
    admin, token, day = _create(app, setup, customer=False)
    assert app.test_client().get(f"/termin/{token}").status_code == 301  # alte persoenliche Links leiten weiter


# --- Freie Zeiten folgen dem Kalender automatisch (kein Freigeben einzelner Uhrzeiten) ------------------


def _offered(client, day, hhmm):
    return f'value="{day.isoformat()}T{hhmm}"' in client.get("/termin/heller").get_data(as_text=True)


def test_internal_event_disappears_at_once_and_returns_after_cancel_move_delete(app, setup):
    admin = _enable(app, setup)
    public = app.test_client()
    monday, tuesday = _next(1), _next(2)
    assert _offered(public, monday, "10:00") and _offered(public, tuesday, "10:00")
    created = admin.post("/kalender/api/termine", json={"title": "Interne Besprechung", "start": f"{monday.isoformat()}T10:00",
                                                        "end": f"{monday.isoformat()}T11:00", "assignee_id": setup.laura_id})
    assert created.status_code == 201, created.get_json()
    event = created.get_json()["event"]
    assert not _offered(public, monday, "10:00") and not _offered(public, monday, "09:30")  # sofort weg (auch Ueberschneidung)
    assert "Interne Besprechung" not in public.get("/termin/heller").get_data(as_text=True)
    # absagen -> wieder buchbar
    cancelled = admin.post(f"/kalender/api/termine/{event['id']}/absagen", json={"version": event["version"]})
    assert cancelled.status_code == 200, cancelled.get_json()
    assert _offered(public, monday, "10:00") and _offered(public, monday, "09:30")
    # reaktivieren und auf Dienstag verschieben -> Montag frei, Dienstag belegt
    event = admin.post(f"/kalender/api/termine/{event['id']}/reaktivieren", json={"version": cancelled.get_json()["event"]["version"]}).get_json()["event"]
    assert not _offered(public, monday, "10:00")
    moved = admin.patch(f"/kalender/api/termine/{event['id']}", json={"title": "Interne Besprechung", "start": f"{tuesday.isoformat()}T10:00",
                                                                     "end": f"{tuesday.isoformat()}T11:00", "assignee_id": setup.laura_id,
                                                                     "version": event["version"]})
    assert moved.status_code == 200, moved.get_json()
    assert _offered(public, monday, "10:00") and not _offered(public, tuesday, "10:00")
    # loeschen -> auch Dienstag wieder frei
    deleted = admin.delete(f"/kalender/api/termine/{event['id']}", json={"version": moved.get_json()["event"]["version"]})
    assert deleted.status_code == 200, deleted.get_json()
    assert _offered(public, tuesday, "10:00")


def test_freed_time_outside_online_windows_stays_hidden(app, setup):
    admin = _enable(app, setup)
    monday = _next(1)
    ev = admin.post("/kalender/api/termine", json={"title": "Nachmittag", "start": f"{monday.isoformat()}T15:00",
                                                   "end": f"{monday.isoformat()}T16:00", "assignee_id": setup.laura_id}).get_json()["event"]
    admin.post(f"/kalender/api/termine/{ev['id']}/absagen", json={"version": ev["version"]})
    assert not _offered(app.test_client(), monday, "15:00")  # Montag nur 09-12 freigegeben
