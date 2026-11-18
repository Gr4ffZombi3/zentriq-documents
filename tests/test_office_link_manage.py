# ruff: noqa: F811  (Fixtures werden aus anderen Testmodulen importiert)
"""Allgemeinen Buero-Link unter "Buchungslinks" verwalten: Adresse aendern, aus-/einschalten. Fiktive Daten."""

import re

from app.extensions import db as _db
from app.models import CalendarEvent, OfficeBookingPage
from app.models.office_settings import OfficeSettings
from app.tenancy import bypass_tenant_scope
from tests.test_booking_links import _csrf
from tests.test_customer_notes import office  # noqa: F401
from tests.test_office_booking import _enable, _next, setup  # noqa: F401
from tests.test_permissions_security import login, world  # noqa: F401


def _page():
    with bypass_tenant_scope():
        _db.session.expire_all()
        return OfficeBookingPage.query.order_by(OfficeBookingPage.id).first()  # Buero A (zuerst angelegt)


def _slot(client, slug):
    html = client.get(f"/termin/{slug}").get_data(as_text=True)
    day = _next(1).isoformat()
    values = re.findall(rf'name="slot" value="({day}T[0-9:]+)"', html)
    return values[0], _csrf(html)


def test_admin_changes_address_old_link_dead_new_link_books_appointments_kept(app, setup):
    admin = _enable(app, setup)
    customer = app.test_client()
    slot, sub = _slot(customer, "heller")
    assert customer.post("/termin/heller", data={"slot": slot, "name": "Kunde Alt", "phone": "0171 1234567", "submission": sub}).status_code == 303

    html = admin.get("/kalender/buchungslinks").get_data(as_text=True)
    assert "Adresse ändern" in html and "data-bl-office-off-btn" in html and 'value="heller"' in html

    r = admin.post("/kalender/buchungslinks/buero/adresse", data={"slug": "  Termin-Heller "})
    assert r.status_code == 302 and r.headers["Location"].endswith("#bl-office")
    assert _page().slug == "termin-heller"
    html = admin.get("/kalender/buchungslinks").get_data(as_text=True)
    assert "https://zentriq.test/termin/termin-heller" in html and "Adresse geändert" in html

    fresh = app.test_client()
    old = fresh.get("/termin/heller")
    assert old.status_code == 404 or "nicht verfügbar" in old.get_data(as_text=True)
    slot2, sub2 = _slot(fresh, "termin-heller")
    assert fresh.post("/termin/termin-heller", data={"slot": slot2, "name": "Kunde Neu", "phone": "0171 7654321", "submission": sub2}).status_code == 303
    with bypass_tenant_scope():
        assert CalendarEvent.query.count() == 2  # alter und neuer Termin bleiben


def test_invalid_or_taken_address_is_refused(app, setup):
    admin = _enable(app, setup)
    for bad in ("", "!!!", "admin"):
        admin.post("/kalender/buchungslinks/buero/adresse", data={"slug": bad})
        assert _page().slug == "heller"
    with bypass_tenant_scope():
        _db.session.add(OfficeBookingPage(tenant_id=setup.tenant_b.id, slug="vergeben", title="X", minutes=30, horizon_days=7))
        _db.session.commit()
    admin.post("/kalender/buchungslinks/buero/adresse", data={"slug": "vergeben"})
    assert _page().slug == "heller"
    assert "bereits vergeben" in admin.get("/kalender/buchungslinks").get_data(as_text=True)


def test_switch_off_and_on(app, setup):
    admin = _enable(app, setup)
    assert admin.post("/kalender/buchungslinks/buero/aktiv", data={"enabled": "0"}).status_code == 302
    assert _page().enabled is False
    assert "nicht verfügbar" in app.test_client().get("/termin/heller").get_data(as_text=True)
    html = admin.get("/kalender/buchungslinks").get_data(as_text=True)
    assert "data-bl-office-off" in html and "data-bl-office-on-btn" in html and "data-bl-office-copy" not in html

    assert admin.post("/kalender/buchungslinks/buero/aktiv", data={"enabled": "1"}).status_code == 302
    assert _page().enabled is True
    assert "data-bl-office-copy" in admin.get("/kalender/buchungslinks").get_data(as_text=True)


def test_switch_on_without_availability_refused(app, setup):
    admin = _enable(app, setup)
    admin.post("/kalender/buchungslinks/buero/aktiv", data={"enabled": "0"})
    with bypass_tenant_scope():
        OfficeSettings.query.filter_by(tenant_id=setup.tenant_a.id).one().hours = None
        _db.session.commit()
    admin.post("/kalender/buchungslinks/buero/aktiv", data={"enabled": "1"})
    assert _page().enabled is False
    assert "keine Verfügbarkeit hinterlegt" in admin.get("/kalender/buchungslinks").get_data(as_text=True)


def test_only_office_admin(app, setup):
    _enable(app, setup)
    laura = login(app, "laura@example.com")  # darf Links verschicken, aber nicht den Buero-Link verwalten
    assert "data-bl-office-manage" not in laura.get("/kalender/buchungslinks").get_data(as_text=True)
    assert laura.post("/kalender/buchungslinks/buero/adresse", data={"slug": "fremd"}).status_code == 403
    assert laura.post("/kalender/buchungslinks/buero/aktiv", data={"enabled": "0"}).status_code == 403
    assert login(app, "admin-b@example.com").post("/kalender/buchungslinks/buero/aktiv", data={"enabled": "0"}).status_code == 404
    page = _page()
    assert page.slug == "heller" and page.enabled
