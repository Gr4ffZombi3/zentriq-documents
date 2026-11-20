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
    assert url.startswith("https://zentriq.test/termin/buchen/"), url  # volle Adresse mit Domain
    token = url.rsplit("/termin/buchen/", 1)[1]
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
    r = anon.get(f"/termin/buchen/{token}")
    html = r.get_data(as_text=True)
    assert r.status_code == 200 and r.headers["Cache-Control"] == "no-store" and "noindex" in r.headers["X-Robots-Tag"]
    times = re.findall(r'name="slot" value="([^"]+)"', html)
    assert times == [_slot(day, "10:00"), _slot(day, "14:00")]
    for secret in ("Geheime", "Müller", "vertraulich", "Karl", "Geheimkunde", "/kalender", "/zentriq", "Abmelden", "@example.com"):
        assert secret not in html, secret
    # Der Name der zustaendigen Person steht bewusst in der Ueberschrift ("Termin bei ... buchen").
    for ident in (setup.karl_id, setup.laura_id, setup.tenant_a.id):  # keine IDs als Wert/Parameter
        assert not re.search(rf'value="{ident}"|[?&][a-z_]+={ident}\b|/{ident}["/]', html), ident
    assert "Beratungsgespräch" in html and "30 Minuten" in html and "Telefon" in html
    assert "name=\"name\"" not in html  # Kundenakte hinterlegt: keine Kontaktabfrage


def test_booking_flow_double_submit_and_internal_visibility(app, setup):
    admin, token, day = _create(app, setup)
    anon = app.test_client()
    sub = _csrf(anon.get(f"/termin/buchen/{token}").get_data(as_text=True))
    r = anon.post(f"/termin/buchen/{token}", data={"slot": _slot(day, "14:00"), "submission": sub, "customer_id": "999", "assignee_id": "1"})
    assert r.status_code == 303
    page = anon.get(f"/termin/buchen/{token}").get_data(as_text=True)
    assert "Ihr Termin ist gebucht" in page and day.strftime("%d.%m.%Y") in page and "14:00" in page and "Beratungsgespräch" in page
    again = anon.post(f"/termin/buchen/{token}", data={"slot": _slot(day, "14:00"), "submission": sub}, follow_redirects=True)
    assert "Ihr Termin ist gebucht" in again.get_data(as_text=True)
    with bypass_tenant_scope():
        [event] = CalendarEvent.query.filter(CalendarEvent.client_ref.like("bl-%")).all()
        link = BookingLink.query.one()
        assert event.customer_id == setup.karl_id and event.assignee_user_id == setup.laura_id  # manipulierte Felder ignoriert
        assert to_local(event.starts_at).strftime("%H:%M") == "14:00" and link.booking_count == 1 and link.used_at
    # anderer Browser: Link genutzt
    other = app.test_client().get(f"/termin/buchen/{token}").get_data(as_text=True)
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
    sub = _csrf(anon.get(f"/termin/buchen/{token}").get_data(as_text=True))
    # Inzwischen bucht das Buero intern 10:00 bei Laura (interner Kalender)
    r = admin.post("/kalender/api/termine", json={"title": "Intern", "start": _slot(day, "10:00"), "end": _slot(day, "10:30"),
                                                   "assignee_id": setup.laura_id})
    assert r.status_code == 201
    r = anon.post(f"/termin/buchen/{token}", data={"slot": _slot(day, "10:00"), "submission": sub})
    html = r.get_data(as_text=True)
    assert r.status_code == 409 and "nicht mehr frei" in html
    assert re.findall(r'name="slot" value="([^"]+)"', html) == [_slot(day, "11:00"), _slot(day, "14:00")]


def test_manipulated_requests(app, setup):
    _admin, token, day = _create(app, setup)
    anon = app.test_client()
    sub = _csrf(anon.get(f"/termin/buchen/{token}").get_data(as_text=True))
    # nicht freigegebene Uhrzeit
    r = anon.post(f"/termin/buchen/{token}", data={"slot": _slot(day, "12:00"), "submission": sub})
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
    html = anon.get(f"/termin/buchen/{token}").get_data(as_text=True)
    assert 'name="name"' in html and 'name="phone"' in html
    sub = _csrf(html)
    r = anon.post(f"/termin/buchen/{token}", data={"slot": _slot(day, "10:00"), "submission": sub, "name": "Eva Probe"})
    assert r.status_code == 409 and "Telefonnummer oder E-Mail" in r.get_data(as_text=True)
    r = anon.post(f"/termin/buchen/{token}", data={"slot": _slot(day, "10:00"), "submission": sub, "name": "Eva Probe", "phone": "0171 1112223"})
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
    assert anon.get(f"/termin/buchen/{token}").status_code == 410
    assert anon.post(f"/termin/buchen/{token}", data={"slot": _slot(day, "10:00")}).status_code == 410
    _admin, token2, _ = _create(app, setup)
    with bypass_tenant_scope():
        link = BookingLink.query.filter(BookingLink.token_hash == bl.token_hash(token2)).one()
        link.expires_at = utcnow_naive() - timedelta(minutes=1)
        _db.session.commit()
    r = anon.get(f"/termin/buchen/{token2}")
    assert r.status_code == 410 and "abgelaufen" in r.get_data(as_text=True)
    assert "abgelaufen" in admin.get("/kalender/buchungslinks").get_data(as_text=True)


def test_all_taken_shows_hint(app, setup):
    admin, token, day = _create(app, setup, slots=("09:00",))
    admin.post("/kalender/api/termine", json={"title": "Intern", "start": _slot(day, "09:00"), "end": _slot(day, "09:30"), "assignee_id": setup.laura_id})
    r = app.test_client().get(f"/termin/buchen/{token}")
    assert "Keine freien Zeiten" in r.get_data(as_text=True)


def test_range_offers():
    first = datetime(2026, 11, 16).date()
    offers = bl.offers_from_range(first, first + timedelta(days=6), {1, 3}, ["09:00", "14:30"])
    assert offers == ["2026-11-16T09:00", "2026-11-16T14:30", "2026-11-18T09:00", "2026-11-18T14:30"]
    with pytest.raises(bl.LinkError):
        bl.offers_from_range(first, first + timedelta(days=200), {1}, ["09:00"])


def _profile(app, setup, name="Laura Beispiel"):
    from app.models import EmployeeProfile

    with bypass_tenant_scope():
        EmployeeProfile.query.filter(EmployeeProfile.user_id == setup.laura_id).one().display_name = name
        s = OfficeSettings.query.filter(OfficeSettings.tenant_id == setup.tenant_a.id).one()
        s.office_name, s.address, s.phone, s.email = "Kundendienstbüro Musterstadt", "Hauptstr. 1, 12345 Musterstadt", "02641 123456", "buero@example.com"
        _db.session.commit()


def _head(html):
    return html.split("</head>", 1)[0]


def test_page_heading_office_profile_and_neutral_preview(app, setup):
    _profile(app, setup)
    _admin, token, day = _create(app, setup)
    html = app.test_client().get(f"/termin/buchen/{token}", headers={"User-Agent": "WhatsApp/2.23.20.0"}).get_data(as_text=True)
    assert "Termin bei Laura Beispiel buchen" in html
    for part in ("Kundendienstbüro Musterstadt", "Hauptstr. 1, 12345 Musterstadt", 'href="tel:02641123456"', "buero@example.com"):
        assert part in html, part
    assert "Wählen Sie eine freie Uhrzeit. Eine Anmeldung ist nicht erforderlich." in html
    assert "Termin verbindlich buchen" in html and "bereitgestellt von Zentriqai" in html
    head = _head(html)
    assert '<meta property="og:title" content="Termin bei Laura Beispiel buchen">' in head
    assert "<title>Termin bei Laura Beispiel buchen</title>" in head
    for secret in ("Karl", "Geheimkunde", "Beratungsgespräch", day.strftime("%d.%m.%Y"), "10:00", "Telefon", token):
        assert secret not in head, secret  # Vorschau ohne Kunden-/Kalenderdaten


def test_heading_never_uses_email_without_profile_name(app, setup):
    _profile(app, setup, name="")
    with bypass_tenant_scope():
        OfficeSettings.query.filter(OfficeSettings.tenant_id == setup.tenant_a.id).one().office_name = "Agentur Test"
        _db.session.commit()
    _admin, token, _day_ = _create(app, setup)
    html = app.test_client().get(f"/termin/buchen/{token}").get_data(as_text=True)
    assert "Termin bei Agentur Test buchen" in html and "laura" not in html.lower()


def test_opening_and_preview_do_not_consume_link(app, setup):
    _admin, token, _day_ = _create(app, setup)
    for ua in ("WhatsApp/2.23.20.0", "facebookexternalhit/1.1", "Mozilla/5.0 (iPhone)"):
        c = app.test_client()
        assert c.get(f"/termin/buchen/{token}", headers={"User-Agent": ua}).status_code == 200
        assert c.head(f"/termin/buchen/{token}", headers={"User-Agent": ua}).status_code == 200
        assert c.get(f"/termin/{token}", headers={"User-Agent": ua}).status_code == 301
    with bypass_tenant_scope():
        link = BookingLink.query.one()
        assert link.booking_count == 0 and link.used_at is None and bl.status(link) == "active"
        assert CalendarEvent.query.filter(CalendarEvent.client_ref.like("bl-%")).count() == 0


def test_legacy_link_redirects_and_still_books(app, setup):
    _admin, token, day = _create(app, setup)
    anon = app.test_client()
    r = anon.get(f"/termin/{token}")
    assert r.status_code == 301 and r.headers["Location"].endswith(f"/termin/buchen/{token}")
    html = anon.get(f"/termin/{token}", follow_redirects=True).get_data(as_text=True)
    assert "Termin verbindlich buchen" in html
    # Formular, das noch unter der alten Adresse offen war
    r = anon.post(f"/termin/{token}", data={"slot": _slot(day, "11:00"), "submission": _csrf(html)})
    assert r.status_code == 303 and r.headers["Location"].endswith(f"/termin/buchen/{token}")
    page = anon.get(r.headers["Location"]).get_data(as_text=True)
    assert "Ihr Termin ist gebucht" in page and "11:00 Uhr" in page and day.strftime("%d.%m.%Y") in page and "Telefon" in page
    # widerrufen/abgelaufen gilt auch fuer alte Links
    _admin2, token2, _ = _create(app, setup)
    with bypass_tenant_scope():
        link = BookingLink.query.filter(BookingLink.token_hash == bl.token_hash(token2)).one()
        link.revoked_at = utcnow_naive()
        _db.session.commit()
    assert anon.get(f"/termin/{token2}", follow_redirects=True).status_code == 410


def test_https_post_with_csrf_and_retry_after_lost_response(app, setup):
    """Regression: Mit no-referrer schickte der Browser keinen Referer, Flask-WTF lehnte jede
    HTTPS-Buchung mit 400 ab. Ausserdem: Antwort geht verloren -> erneutes Absenden zeigt die
    bereits gespeicherte Buchung, ohne zweiten Termin."""
    _admin, token, day = _create(app, setup, customer=False)
    app.config["WTF_CSRF_ENABLED"] = True
    try:
        from flask import g

        g.pop("csrf_token", None)  # Test-App-Kontext teilt g mit den Requests
        https = "https://zentriq.test"
        anon = app.test_client()
        r = anon.get(f"/termin/buchen/{token}", base_url=https)
        html = r.get_data(as_text=True)
        assert r.headers["Referrer-Policy"] == "same-origin" and '<meta name="referrer" content="same-origin">' in html
        csrf = re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)
        data = {"csrf_token": csrf, "slot": _slot(day, "10:00"), "submission": _csrf(html), "name": "Eva Probe", "phone": "0171 1112223"}
        referer = {"Referer": f"{https}/termin/buchen/{token}"}  # sendet der Browser bei same-origin
        assert anon.post(f"/termin/buchen/{token}", data=data, base_url=https).status_code == 400  # ohne Referer
        r = anon.post(f"/termin/buchen/{token}", data=data, base_url=https, headers=referer)
        assert r.status_code == 303
        # Verbindungsabbruch: Bestaetigung (Session-Eintrag) kam nie beim Browser an
        with anon.session_transaction() as sess:
            for key in [k for k in sess if k.startswith("bl_ok_")]:
                sess.pop(key)
        r = anon.post(f"/termin/buchen/{token}", data=data, base_url=https, headers=referer, follow_redirects=True)
        page = r.get_data(as_text=True)
        assert r.status_code == 200 and "Ihr Termin ist gebucht" in page and "10:00 Uhr" in page
        with bypass_tenant_scope():
            assert CalendarEvent.query.filter(CalendarEvent.client_ref.like("bl-%")).count() == 1
            assert BookingLink.query.one().booking_count == 1
    finally:
        app.config["WTF_CSRF_ENABLED"] = False


# --- Freigabe fuer Mitarbeiter (Laura) ---------------------------------------------------------


def _offers_for_link(token):
    with bypass_tenant_scope():
        return BookingLink.query.filter(BookingLink.token_hash == bl.token_hash(token)).one()


def test_employee_needs_admin_release(app, setup):
    laura = login(app, "laura@example.com")
    assert laura.get("/kalender/buchungslinks").status_code == 403
    assert laura.post("/kalender/buchungslinks", data={"title": "x", "assignee_id": str(setup.admin_a_id)}).status_code == 403
    assert "Buchungslink erstellen" not in laura.get("/kalender").get_data(as_text=True)
    assert laura.get(f"/zentriq/api/kunden/{setup.karl_id}").get_json()["card"]["booking_link_url"] is None
    # Laura kann sich nicht selbst freischalten, Dennis bleibt ohne Freigabe
    assert laura.post(f"/mitarbeiter/{setup.laura_id}/buchungslinks", data={"allowed": "1"}).status_code == 403
    admin = login(app, "admin-a@example.com")
    r = admin.post(f"/mitarbeiter/{setup.laura_id}/buchungslinks", data={"allowed": "1"}, follow_redirects=True)
    assert r.status_code == 200 and "Freigabe entziehen" in r.get_data(as_text=True)
    assert login(app, "dennis@example.com").get("/kalender/buchungslinks").status_code == 403
    # Admin eines anderen Bueros kann Laura nicht freischalten
    assert login(app, "admin-b@example.com").post(f"/mitarbeiter/{setup.laura_id}/buchungslinks", data={"allowed": "0"}).status_code == 404
    admin.post(f"/mitarbeiter/{setup.laura_id}/buchungslinks", data={"allowed": "0"})
    assert login(app, "laura@example.com").get("/kalender/buchungslinks").status_code == 403


def test_employee_link_uses_office_hours_and_books_into_admin_calendar(app, setup):
    admin = login(app, "admin-a@example.com")
    admin.post(f"/mitarbeiter/{setup.laura_id}/buchungslinks", data={"allowed": "1"})
    admin_link_admin, admin_token, _ = _create(app, setup)  # Link des Admins - fuer Laura unsichtbar
    laura = login(app, "laura@example.com")
    assert "Buchungslink erstellen" in laura.get("/kalender").get_data(as_text=True)
    card = laura.get(f"/zentriq/api/kunden/{setup.karl_id}").get_json()["card"]  # Kundenkarte im Hauptbildschirm
    assert card["booking_link_url"] == f"/kalender/buchungslinks?kunde={setup.karl_id}"
    page = laura.get(f"/kalender/buchungslinks?kunde={setup.karl_id}").get_data(as_text=True)
    assert 'name="slots"' not in page and 'name="minutes"' not in page and "data-bl-office-hours" in page
    assert f'value="{setup.laura_id}"' not in page  # nur Buero-Admins waehlbar
    # Manipulierte Felder: eigene Zeiten/Dauer/Mitarbeiter werden nicht uebernommen
    bad = laura.post("/kalender/buchungslinks", data={"title": "Beratung", "assignee_id": str(setup.laura_id)}, follow_redirects=True)
    assert "Büro-Admin auswählen" in bad.get_data(as_text=True)
    with bypass_tenant_scope():
        other_customer = setup.heller_b
    bad = laura.post("/kalender/buchungslinks", data={"title": "Beratung", "assignee_id": str(setup.admin_a_id), "customer_id": str(other_customer)},
                     follow_redirects=True)
    assert "Kunde wurde nicht gefunden" in bad.get_data(as_text=True)
    r = laura.post("/kalender/buchungslinks", follow_redirects=True, data={
        "title": "Beratungsgespräch", "location_kind": "telefon", "assignee_id": str(setup.admin_a_id), "customer_id": str(setup.karl_id),
        "minutes": "200", "mode": "slots", "slots": [f"{_day().isoformat()}T23:30"], "valid_until": "2099-01-01"})
    html = r.get_data(as_text=True)
    url = re.search(r'data-bl-url>([^<]+)<', html).group(1)
    assert url.startswith("https://zentriq.test/termin/buchen/")
    token = url.rsplit("/", 1)[1]
    link = _offers_for_link(token)
    assert link.minutes == 30 and link.assignee_user_id == setup.admin_a_id and link.created_by_user_id == setup.laura_id
    # allgemeiner Link: keine gespeicherten Zeiten, kein Ablauf - freie Zeiten live aus dem Kalender
    assert link.kind == "open" and link.offers == [] and bl.status(link) == "active"
    with bypass_tenant_scope():
        from app.tenancy import use_tenant_id
        with use_tenant_id(setup.tenant_a.id):
            free = [to_local(t).strftime("%H:%M") for t in bl.free_times(link)]
    assert free and all("08:00" <= t <= "17:30" for t in free) and "23:30" not in free
    assert admin_token not in html and html.count("data-bl-row=") == 1  # nur eigene Links
    # Laura kann den Link des Admins nicht widerrufen
    admin_link = _offers_for_link(admin_token)
    assert laura.post(f"/kalender/buchungslinks/{admin_link.id}/widerrufen").status_code == 403
    # abgemeldet oeffnen und buchen - derselbe Ablauf wie beim Admin
    anon = app.test_client()
    page = anon.get(f"/termin/buchen/{token}").get_data(as_text=True)
    slot = re.findall(r'name="slot" value="([^"]+)"', page)[0]
    r = anon.post(f"/termin/buchen/{token}", data={"slot": slot, "submission": _csrf(page)})
    assert r.status_code == 303 and "Ihr Termin ist gebucht" in anon.get(r.headers["Location"]).get_data(as_text=True)
    day = slot[:10]
    events = admin.get(f"/kalender/api/termine?von={day}&bis={day}").get_json()["events"]
    mine = [e for e in events if e["title"] == "Beratungsgespräch – Karl Geheimkunde"]
    assert len(mine) == 1 and mine[0]["start"].startswith(slot)
    with bypass_tenant_scope():
        event = CalendarEvent.query.filter(CalendarEvent.client_ref.like(f"blo-{link.id}-%")).one()
        assert event.assignee_user_id == setup.admin_a_id and event.customer_id == setup.karl_id



# --- Allgemeiner Link (kind "open"): dieselbe URL fuer mehrere Kunden -------------------------------

def _open_link(app, setup):
    """Allgemeinen Link wie Laura erstellen (Mitarbeiterin mit Freigabe, Kalender des Admins)."""
    admin = login(app, "admin-a@example.com")
    admin.post(f"/mitarbeiter/{setup.laura_id}/buchungslinks", data={"allowed": "1"})
    laura = login(app, "laura@example.com")
    html = laura.post("/kalender/buchungslinks", follow_redirects=True,
                      data={"title": "Beratungsgespräch", "assignee_id": str(setup.admin_a_id)}).get_data(as_text=True)
    url = re.search(r'data-bl-url>([^<]+)<', html).group(1)
    return admin, url.rsplit("/termin/buchen/", 1)[1]


def _slots(client, token):
    return re.findall(r'name="slot" value="([^"]+)"', client.get(f"/termin/buchen/{token}").get_data(as_text=True))


def _book_open(client, token, slot, name):
    page = client.get(f"/termin/buchen/{token}").get_data(as_text=True)
    return client.post(f"/termin/buchen/{token}", data={"slot": slot, "submission": _csrf(page), "name": name, "phone": "0171 1234567"})


def test_same_url_books_two_customers_and_booked_time_disappears(app, setup):
    admin, token = _open_link(app, setup)
    first, second = app.test_client(), app.test_client()  # zwei getrennte Browsersitzungen
    slots = _slots(first, token)
    assert len(slots) > 10
    r1 = _book_open(first, token, slots[0], "Kundin Eins")
    assert r1.status_code == 303 and "Ihr Termin ist gebucht" in first.get(r1.headers["Location"]).get_data(as_text=True)
    # derselbe Link: kein "Bereits gebucht", nur die gebuchte Zeit fehlt
    html = second.get(f"/termin/buchen/{token}").get_data(as_text=True)
    assert "Bereits gebucht" not in html
    now_free = _slots(second, token)
    assert slots[0] not in now_free  # gebuchte Zeit faellt weg
    other = slots[-1]  # weit entfernt (direkt folgende Zeit blockiert der Puffer von 15 Minuten)
    assert other in now_free
    r2 = _book_open(second, token, other, "Kunde Zwei")
    assert r2.status_code == 303
    # auch die erste Sitzung sieht beim erneuten Oeffnen wieder die freie Auswahl (keine alte Bestaetigung)
    again = first.get(f"/termin/buchen/{token}").get_data(as_text=True)
    assert "Ihr Termin ist gebucht" not in again
    assert slots[0] not in _slots(first, token) and other not in _slots(first, token)
    with bypass_tenant_scope():
        link = BookingLink.query.filter(BookingLink.token_hash == bl.token_hash(token)).one()
        events = CalendarEvent.query.filter(CalendarEvent.client_ref.like(f"blo-{link.id}-%")).order_by(CalendarEvent.starts_at).all()
        assert [e.title for e in events] == ["Beratungsgespräch – Kundin Eins", "Beratungsgespräch – Kunde Zwei"]
        assert all(e.assignee_user_id == setup.admin_a_id for e in events)  # im richtigen Kalender
        assert [to_local(e.starts_at).strftime("%Y-%m-%dT%H:%M") for e in events] == [slots[0], other]
        assert link.booking_count == 2 and bl.status(link) == "active"
    # Buero sieht die Buchungen in der Linkliste
    assert "2 Buchungen" in login(app, "laura@example.com").get("/kalender/buchungslinks").get_data(as_text=True)
    # Absage gibt die Zeit wieder frei
    with bypass_tenant_scope():
        events[0].status = "cancelled"
        _db.session.commit()
    assert slots[0] in _slots(app.test_client(), token)


def test_double_submit_on_open_link_gives_one_event(app, setup):
    _admin, token = _open_link(app, setup)
    client = app.test_client()
    page = client.get(f"/termin/buchen/{token}").get_data(as_text=True)
    slot, sub = re.findall(r'name="slot" value="([^"]+)"', page)[0], _csrf(page)
    data = {"slot": slot, "submission": sub, "name": "Kundin Eins", "phone": "0171 1234567"}
    assert client.post(f"/termin/buchen/{token}", data=data).status_code == 303
    assert client.post(f"/termin/buchen/{token}", data=data).status_code == 303  # Antwort verloren, erneut gesendet
    with bypass_tenant_scope():
        assert CalendarEvent.query.filter(CalendarEvent.title == "Beratungsgespräch – Kundin Eins").count() == 1


def test_two_simultaneous_bookings_for_same_time_store_exactly_one(app, setup):
    import threading

    _admin, token = _open_link(app, setup)
    clients = [app.test_client(), app.test_client()]
    pages = [c.get(f"/termin/buchen/{token}").get_data(as_text=True) for c in clients]
    slot = re.findall(r'name="slot" value="([^"]+)"', pages[0])[0]
    results, barrier = [None, None], threading.Barrier(2)

    def go(i):
        barrier.wait()
        r = clients[i].post(f"/termin/buchen/{token}", data={"slot": slot, "submission": _csrf(pages[i]), "name": f"Kunde {i}", "phone": "0171 1234567"})
        results[i] = (r.status_code, r.get_data(as_text=True))

    threads = [threading.Thread(target=go, args=(i,)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    codes = sorted(r[0] for r in results)
    assert codes == [303, 409], codes  # einer bucht, der andere bekommt die aktualisierte Auswahl
    loser = next(r[1] for r in results if r[0] == 409)
    assert "nicht mehr frei" in loser and f'value="{slot}"' not in loser
    with bypass_tenant_scope():
        start = local_to_utc_naive(datetime.fromisoformat(slot).date(), datetime.fromisoformat(slot).time())
        assert CalendarEvent.query.filter(CalendarEvent.starts_at == start, CalendarEvent.status == "scheduled").count() == 1


def test_already_sent_general_link_keeps_working_after_switch(app, setup):
    """Wie der bestehende Link aus der Produktion: frueher als Einmal-Link gespeichert, schon gebucht und
    mit Ablaufdatum - nach der Umstellung (Migration: kind="open") bucht dieselbe URL weiter."""
    _admin, token = _open_link(app, setup)
    with bypass_tenant_scope():
        link = BookingLink.query.filter(BookingLink.token_hash == bl.token_hash(token)).one()
        link.booking_count, link.used_at, link.max_bookings = 1, utcnow_naive(), 1
        link.expires_at = utcnow_naive() - timedelta(days=1)
        link.offers = ["2020-01-01T09:00"]  # alte gespeicherte Zeiten spielen keine Rolle mehr
        _db.session.commit()
    client = app.test_client()
    html = client.get(f"/termin/buchen/{token}").get_data(as_text=True)
    assert "Bereits gebucht" not in html and "abgelaufen" not in html
    slot = _slots(client, token)[0]
    assert _book_open(client, token, slot, "Neuer Kunde").status_code == 303


def test_open_link_follows_calendar_availability_and_revoke_still_works(app, setup):
    admin, token = _open_link(app, setup)
    day = local_today() + timedelta(days=2)
    while day.isoweekday() > 5:  # Werktag (Arbeitstage des Kalenders), sonst am Freitag/Samstag ausgefuehrt leer
        day += timedelta(days=1)
    with bypass_tenant_scope():
        s = OfficeSettings.query.filter_by(tenant_id=setup.tenant_a.id).one()
        hours = dict(s.hours)
        hours.pop(str(day.isoweekday()))  # an diesem Wochentag keine Buerozeit
        s.hours = hours
        _db.session.commit()
    assert not any(x.startswith(day.isoformat()) for x in _slots(app.test_client(), token))
    with bypass_tenant_scope():
        s = OfficeSettings.query.filter_by(tenant_id=setup.tenant_a.id).one()
        s.hours = {**s.hours, str(day.isoweekday()): ["13:00", "14:00"]}
        _db.session.commit()
    mine = [x for x in _slots(app.test_client(), token) if x.startswith(day.isoformat())]
    assert mine == [f"{day.isoformat()}T13:00", f"{day.isoformat()}T13:30"]
    with bypass_tenant_scope():
        link = BookingLink.query.filter(BookingLink.token_hash == bl.token_hash(token)).one()
    login(app, "laura@example.com").post(f"/kalender/buchungslinks/{link.id}/widerrufen")
    assert app.test_client().get(f"/termin/buchen/{token}").status_code == 410


def test_proposal_links_keep_single_booking(app, setup):
    _admin, token, day = _create(app, setup, customer=False)
    with bypass_tenant_scope():
        assert BookingLink.query.filter(BookingLink.token_hash == bl.token_hash(token)).one().kind == "proposal"
    client = app.test_client()
    assert _book_open(client, token, _slot(day, "10:00"), "Kundin Eins").status_code == 303
    other = app.test_client().get(f"/termin/buchen/{token}").get_data(as_text=True)
    assert "Bereits gebucht" in other
