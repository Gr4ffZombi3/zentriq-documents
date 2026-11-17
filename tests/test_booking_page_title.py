# ruff: noqa: F811  (Fixtures werden aus anderen Testmodulen importiert)
"""Eigener Titel der oeffentlichen Buchungsseite (Kalender > Einstellungen > Online-Buchung)."""

import re

from app.extensions import db as _db
from app.models import CalendarEvent
from app.models.office_settings import OfficeSettings
from app.tenancy import bypass_tenant_scope
from tests.test_booking_links import _csrf, _open_link, _slots, no_ai, setup  # noqa: F401
from tests.test_customer_notes import office  # noqa: F401
from tests.test_permissions_security import login, world  # noqa: F401

TITLE = "Termin bei der HUK-COBURG – Justin Heller buchen"


def _save(client, setup, page_title, **extra):
    data = {"enabled": "1", "slug": "agentur-test", "title": "Beratungstermin", "minutes": "30",
            "assignee_id": str(setup.admin_a_id), "horizon_days": "14", "page_title": page_title, **extra}
    return client.post("/kalender/einstellungen/online-buchung", data=data)


def _h1_and_tab(html):
    return (re.search(r"<h1>\s*(.*?)\s*</h1>", html, re.S).group(1).strip(),
            re.search(r"<title>(.*?)</title>", html, re.S).group(1).strip())


def _stored(setup):
    with bypass_tenant_scope():
        _db.session.expire_all()
        return OfficeSettings.query.filter_by(tenant_id=setup.tenant_a.id).one().booking_title


def test_save_change_and_already_sent_link_shows_new_title_and_still_books(app, setup):
    _laura_admin, token = _open_link(app, setup)  # bereits verschickter allgemeiner Link
    customer = app.test_client()
    slots = _slots(customer, token)
    assert customer.post(f"/termin/buchen/{token}", data={"slot": slots[0], "submission": _csrf(customer.get(f"/termin/buchen/{token}").get_data(as_text=True)),
                                                         "name": "Kundin Vorher", "phone": "0171 1234567"}).status_code == 303
    default_h1, default_tab = _h1_and_tab(app.test_client().get(f"/termin/buchen/{token}").get_data(as_text=True))
    assert TITLE not in default_h1

    admin = login(app, "admin-a@example.com")
    assert _save(admin, setup, TITLE).status_code == 302
    assert _stored(setup) == TITLE
    settings_html = admin.get("/kalender/einstellungen").get_data(as_text=True)
    assert "Titel der Buchungsseite" in settings_html and 'value="Termin bei der HUK-COBURG – Justin Heller buchen"' in settings_html

    fresh = app.test_client()  # ohne Login, gleiche URL
    page = fresh.get(f"/termin/buchen/{token}")
    assert page.status_code == 200
    assert _h1_and_tab(page.get_data(as_text=True)) == (TITLE, TITLE)
    assert f'og:title" content="{TITLE}"' in page.get_data(as_text=True)
    office_page = fresh.get("/termin/agentur-test").get_data(as_text=True)
    assert _h1_and_tab(office_page) == (TITLE, TITLE)

    # spaeter erneut aendern
    assert _save(admin, setup, "  Online-Termin   bei Justin Heller ").status_code == 302
    assert _stored(setup) == "Online-Termin bei Justin Heller"
    html = fresh.get(f"/termin/buchen/{token}").get_data(as_text=True)
    assert _h1_and_tab(html) == ("Online-Termin bei Justin Heller", "Online-Termin bei Justin Heller")
    # weiterhin buchbar, alte Buchung bleibt
    slot = _slots(fresh, token)[-1]
    r = fresh.post(f"/termin/buchen/{token}", data={"slot": slot, "submission": _csrf(html), "name": "Kunde Nachher", "phone": "0171 7654321"})
    assert r.status_code == 303 and "Ihr Termin ist gebucht" in fresh.get(r.headers["Location"]).get_data(as_text=True)
    with bypass_tenant_scope():
        titles = {e.title for e in CalendarEvent.query.all()}
    assert {"Beratungsgespräch – Kundin Vorher", "Beratungsgespräch – Kunde Nachher"} <= titles

    # leer = Standardtitel
    assert _save(admin, setup, "   ").status_code == 302
    assert _stored(setup) is None
    assert _h1_and_tab(app.test_client().get(f"/termin/buchen/{token}").get_data(as_text=True)) == (default_h1, default_tab)


def test_only_office_admin_may_change_title(app, setup):
    laura = login(app, "laura@example.com")
    assert _save(laura, setup, "Gekapert").status_code == 403
    assert laura.get("/kalender/einstellungen").status_code == 403
    assert _stored(setup) is None


def test_title_is_escaped_and_limited(app, setup):
    admin = login(app, "admin-a@example.com")
    assert _save(admin, setup, "<script>alert(1)</script>" + "x" * 200).status_code == 302
    stored = _stored(setup)
    assert len(stored) == 120
    html = app.test_client().get("/termin/agentur-test").get_data(as_text=True)
    assert "<script>alert(1)" not in html and "&lt;script&gt;" in html
