# ruff: noqa: F811  (Fixtures werden aus anderen Testmodulen importiert)
"""Persoenliche Buchungslinks: Buero (Erstellen/Status/Widerruf) und oeffentliche Seite (ohne
Anmeldung). Synthetische Daten, KI-Anbieter aus."""

import re
from datetime import datetime, time, timedelta

import pytest

from app.extensions import db as _db
from app.models import BookingLink, CalendarEvent, Customer
from app.models.ai import AiUsage
from app.models.office_settings import OfficeSettings
from app.services import booking_links as bl
from app.services.ai import adapters
from app.services.timetracking.clock import local_to_utc_naive, local_today, to_local, utcnow_naive
from app.tenancy import bypass_tenant_scope
from tests.test_customer_notes import office  # noqa: F401
from tests.test_permissions_security import login, world  # noqa: F401


@pytest.fixture(autouse=True)
def no_ai(monkeypatch):
    def forbidden(*a, **k):
        raise AssertionError("Modellaufruf ist hier nicht erlaubt")

    monkeypatch.setattr(adapters, "call", forbidden)


@pytest.fixture()
def setup(app, office):
    with bypass_tenant_scope():
        _db.session.add(OfficeSettings(tenant_id=office.tenant_a.id, hours={str(d): ["08:00", "18:00"] for d in range(1, 8)},
                                       default_minutes=30, buffer_minutes=15, lead_minutes=0, office_name="Agentur Test"))
        karl = Customer(tenant_id=office.tenant_a.id, name="Karl Geheimkunde", name_key="karl geheimkunde", assigned_user_id=office.laura_id)
        _db.session.add(karl)
        _db.session.commit()
        office.karl_id = karl.id
    return office


DAY = None


def _day(n=3):
    return local_today() + timedelta(days=n)


def _create(app, setup, slots=("10:00", "11:00", "14:00"), customer=True, valid_days=10):
    admin = login(app, "admin-a@example.com")
    day = _day()
    data = {"title": "Beratungsgespräch", "location_kind": "telefon", "minutes": "30", "assignee_id": str(setup.laura_id),
            "valid_until": (local_today() + timedelta(days=valid_days)).isoformat(), "mode": "slots",
            "slots": [f"{day.isoformat()}T{s}" for s in slots]}
    if customer:
        data["customer_id"] = str(setup.karl_id)
    r = admin.post("/kalender/buchungslinks", data=data, follow_redirects=True)
    assert r.status_code == 200, r.status_code
    html = r.get_data(as_text=True)
    url = re.search(r'data-bl-url>([^<]+)<', html).group(1)
    token = url.rsplit("/termin/", 1)[1]
    return admin, token, day


def _csrf(html):
    m = re.search(r'name="submission" value="([^"]+)"', html)
    return m.group(1) if m else None


def _slot(day, hhmm):
    return f"{day.isoformat()}T{hhmm}"


def test_only_office_admin_creates_and_link_has_no_ids(app, setup):
    assert login(app, "laura@example.com").get("/kalender/buchungslinks").status_code == 403
    assert login(app, "laura@example.com").post("/kalender/buchungslinks", data={"title": "x"}).status_code == 403
    admin, token, _day_ = _create(app, setup)
    assert re.fullmatch(r"[A-Za-z0-9_-]{43}", token)  # secrets.token_urlsafe(32): 256 Bit, keine IDs
    _admin2, token2, _ = _create(app, setup)
    assert token2 != token
    with bypass_tenant_scope():
        link = BookingLink.query.filter(BookingLink.token_hash == bl.token_hash(token)).one()
    assert link.token_hash == bl.token_hash(token) and token not in (link.token_enc or "")  # Klartext nicht gespeichert
    page = admin.get("/kalender/buchungslinks").get_data(as_text=True)
    assert "aktiv" in page and "Karl Geheimkunde" in page and "3 Zeiten frei" in page


def test_public_page_shows_only_released_free_times_and_no_internal_data(app, setup):
    admin, token, day = _create(app, setup)
    # Interner Termin 11:20-11:40 -> 11:00 (30 Min + 15 Min Puffer) und 10:00? 10:00-10:30+15=10:45 < 11:20 frei
    with bypass_tenant_scope():
        start = local_to_utc_naive(day, time(11, 20))
        _db.session.add(CalendarEvent(tenant_id=setup.tenant_a.id, title="Geheime Besprechung Müller", starts_at=start,
                                      ends_at=start + timedelta(minutes=20), assignee_user_id=setup.laura_id, created_by_user_id=setup.admin_a_id,
                                      note="vertraulich", status="scheduled"))
        _db.session.commit()
    anon = app.test_client()
    r = anon.get(f"/termin/{token}")
    html = r.get_data(as_text=True)
    assert r.status_code == 200 and r.headers["Cache-Control"] == "no-store" and "noindex" in r.headers["X-Robots-Tag"]
    times = re.findall(r'name="slot" value="([^"]+)"', html)
    assert times == [_slot(day, "10:00"), _slot(day, "14:00")]
    for secret in ("Geheime", "Müller", "vertraulich", "Karl", "Geheimkunde", "Laura", "/kalender", "/zentriq", "Abmelden"):
        assert secret not in html, secret
    for ident in (setup.karl_id, setup.laura_id, setup.tenant_a.id):  # keine IDs als Wert/Parameter
        assert not re.search(rf'value="{ident}"|[?&][a-z_]+={ident}\b|/{ident}["/]', html), ident
    assert "Beratungsgespräch" in html and "30 Minuten" in html and "Telefon" in html
    assert "name=\"name\"" not in html  # Kundenakte hinterlegt: keine Kontaktabfrage


def test_booking_flow_double_submit_and_internal_visibility(app, setup):
    admin, token, day = _create(app, setup)
    anon = app.test_client()
    sub = _csrf(anon.get(f"/termin/{token}").get_data(as_text=True))
    r = anon.post(f"/termin/{token}", data={"slot": _slot(day, "14:00"), "submission": sub, "customer_id": "999", "assignee_id": "1"})
    assert r.status_code == 303
    page = anon.get(f"/termin/{token}").get_data(as_text=True)
    assert "Ihr Termin ist gebucht" in page and day.strftime("%d.%m.%Y") in page and "14:00" in page and "Beratungsgespräch" in page
    again = anon.post(f"/termin/{token}", data={"slot": _slot(day, "14:00"), "submission": sub}, follow_redirects=True)
    assert "Ihr Termin ist gebucht" in again.get_data(as_text=True)
    with bypass_tenant_scope():
        [event] = CalendarEvent.query.filter(CalendarEvent.client_ref.like("bl-%")).all()
        link = BookingLink.query.one()
        assert event.customer_id == setup.karl_id and event.assignee_user_id == setup.laura_id  # manipulierte Felder ignoriert
        assert to_local(event.starts_at).strftime("%H:%M") == "14:00" and link.booking_count == 1 and link.used_at
    # anderer Browser: Link genutzt
    other = app.test_client().get(f"/termin/{token}").get_data(as_text=True)
    assert "Bereits gebucht" in other and "14:00" not in other
    # Buero sieht den Termin und den Status "genutzt"
    events = login(app, "laura@example.com").get(f"/kalender/api/termine?von={day.isoformat()}&bis={day.isoformat()}").get_json()["events"]
    assert [e["title"] for e in events] == ["Beratungsgespräch – Karl Geheimkunde"]
    assert "genutzt" in admin.get("/kalender/buchungslinks").get_data(as_text=True)
    with bypass_tenant_scope():
        assert AiUsage.query.count() == 0


def test_slot_taken_meanwhile_shows_remaining_times(app, setup):
    admin, token, day = _create(app, setup)
    anon = app.test_client()
    sub = _csrf(anon.get(f"/termin/{token}").get_data(as_text=True))
    # Inzwischen bucht das Buero intern 10:00 bei Laura (interner Kalender)
    r = admin.post("/kalender/api/termine", json={"title": "Intern", "start": _slot(day, "10:00"), "end": _slot(day, "10:30"),
                                                   "assignee_id": setup.laura_id})
    assert r.status_code == 201
    r = anon.post(f"/termin/{token}", data={"slot": _slot(day, "10:00"), "submission": sub})
    html = r.get_data(as_text=True)
    assert r.status_code == 409 and "nicht mehr frei" in html
    assert re.findall(r'name="slot" value="([^"]+)"', html) == [_slot(day, "11:00"), _slot(day, "14:00")]


def test_manipulated_requests(app, setup):
    _admin, token, day = _create(app, setup)
    anon = app.test_client()
    sub = _csrf(anon.get(f"/termin/{token}").get_data(as_text=True))
    # nicht freigegebene Uhrzeit
    r = anon.post(f"/termin/{token}", data={"slot": _slot(day, "12:00"), "submission": sub})
    assert r.status_code == 409 and "nicht freigegeben" in r.get_data(as_text=True)
    # fremdes/geratenes Token, IDs im Pfad
    for path in (f"/termin/{token[:-1]}x", "/termin/1", f"/termin/{setup.karl_id}", "/termin/../kalender"):
        assert app.test_client().get(path).status_code in (404, 308)
    # oeffentlicher Link oeffnet keine internen Bereiche
    for path in ("/kalender/api/termine", f"/zentriq/api/kunden/{setup.karl_id}", "/kalender/buchungslinks", f"/customers/{setup.karl_id}"):
        r = anon.get(path, headers={"Accept": "application/json"})
        assert r.status_code in (401, 302), (path, r.status_code)
    with bypass_tenant_scope():
        assert CalendarEvent.query.filter(CalendarEvent.client_ref.like("bl-%")).count() == 0


def test_contact_required_without_customer(app, setup):
    _admin, token, day = _create(app, setup, customer=False)
    anon = app.test_client()
    html = anon.get(f"/termin/{token}").get_data(as_text=True)
    assert 'name="name"' in html and 'name="phone"' in html
    sub = _csrf(html)
    r = anon.post(f"/termin/{token}", data={"slot": _slot(day, "10:00"), "submission": sub, "name": "Eva Probe"})
    assert r.status_code == 409 and "Telefonnummer oder E-Mail" in r.get_data(as_text=True)
    r = anon.post(f"/termin/{token}", data={"slot": _slot(day, "10:00"), "submission": sub, "name": "Eva Probe", "phone": "0171 1112223"})
    assert r.status_code == 303
    with bypass_tenant_scope():
        event = CalendarEvent.query.filter(CalendarEvent.client_ref.like("bl-%")).one()
    assert event.customer_id is None and "Eva Probe" in event.title and "0171 1112223" in event.note


def test_revoked_and_expired(app, setup):
    admin, token, day = _create(app, setup)
    with bypass_tenant_scope():
        link_id = BookingLink.query.one().id
    r = admin.post(f"/kalender/buchungslinks/{link_id}/widerrufen", follow_redirects=True)
    assert "widerrufen" in r.get_data(as_text=True)
    anon = app.test_client()
    assert anon.get(f"/termin/{token}").status_code == 410
    assert anon.post(f"/termin/{token}", data={"slot": _slot(day, "10:00")}).status_code == 410
    _admin, token2, _ = _create(app, setup)
    with bypass_tenant_scope():
        link = BookingLink.query.filter(BookingLink.token_hash == bl.token_hash(token2)).one()
        link.expires_at = utcnow_naive() - timedelta(minutes=1)
        _db.session.commit()
    r = anon.get(f"/termin/{token2}")
    assert r.status_code == 410 and "abgelaufen" in r.get_data(as_text=True)
    assert "abgelaufen" in admin.get("/kalender/buchungslinks").get_data(as_text=True)


def test_all_taken_shows_hint(app, setup):
    admin, token, day = _create(app, setup, slots=("09:00",))
    admin.post("/kalender/api/termine", json={"title": "Intern", "start": _slot(day, "09:00"), "end": _slot(day, "09:30"), "assignee_id": setup.laura_id})
    r = app.test_client().get(f"/termin/{token}")
    assert "Keine freien Zeiten" in r.get_data(as_text=True)


def test_range_offers():
    first = datetime(2026, 11, 16).date()
    offers = bl.offers_from_range(first, first + timedelta(days=6), {1, 3}, ["09:00", "14:30"])
    assert offers == ["2026-11-16T09:00", "2026-11-16T14:30", "2026-11-18T09:00", "2026-11-18T14:30"]
    with pytest.raises(bl.LinkError):
        bl.offers_from_range(first, first + timedelta(days=200), {1}, ["09:00"])
