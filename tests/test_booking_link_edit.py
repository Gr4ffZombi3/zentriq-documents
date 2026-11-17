# ruff: noqa: F811  (Fixtures werden aus anderen Testmodulen importiert)
"""Buchungslinks umbenennen und loeschen (Seite Buchungslinks)."""

import re

from app.models import BookingLink, CalendarEvent
from app.services import booking_links as bl
from app.tenancy import bypass_tenant_scope
from tests.test_booking_links import _create, _csrf, _open_link, _slot, _slots, no_ai, setup  # noqa: F401
from tests.test_customer_notes import office  # noqa: F401
from tests.test_permissions_security import login, world  # noqa: F401


def _link(token):
    with bypass_tenant_scope():
        return BookingLink.query.filter(BookingLink.token_hash == bl.token_hash(token)).one()


def _book(client, token, slot, name):
    page = client.get(f"/termin/buchen/{token}").get_data(as_text=True)
    return client.post(f"/termin/buchen/{token}", data={"slot": slot, "submission": _csrf(page), "name": name, "phone": "0171 1234567"})


def test_rename_keeps_url_and_bookings_and_shows_new_name(app, setup):
    admin, token = _open_link(app, setup)
    laura = login(app, "laura@example.com")
    link = _link(token)
    customer = app.test_client()
    slots = _slots(customer, token)
    assert _book(customer, token, slots[0], "Kundin Eins").status_code == 303
    r = laura.post(f"/kalender/buchungslinks/{link.id}/name", data={"title": "  Kfz-Beratung   online "})
    assert r.status_code == 302
    assert _link(token).title == "Kfz-Beratung online" and _link(token).token_hash == link.token_hash
    html = laura.get("/kalender/buchungslinks").get_data(as_text=True)
    assert "Kfz-Beratung online" in html and "Umbenennen" in html and "Löschen" in html
    page = app.test_client().get(f"/termin/buchen/{token}").get_data(as_text=True)  # gleiche URL, ohne Login
    assert "Kfz-Beratung online" in page and "Beratungsgespräch" not in page
    assert _book(app.test_client(), token, slots[-1], "Kunde Zwei").status_code == 303
    with bypass_tenant_scope():
        assert CalendarEvent.query.filter(CalendarEvent.title == "Beratungsgespräch – Kundin Eins").count() == 1
        assert CalendarEvent.query.filter(CalendarEvent.title == "Kfz-Beratung online – Kunde Zwei").count() == 1
    # spaeter erneut aendern; leer abgelehnt
    laura.post(f"/kalender/buchungslinks/{link.id}/name", data={"title": "Rentenberatung"})
    assert _link(token).title == "Rentenberatung"
    r = laura.post(f"/kalender/buchungslinks/{link.id}/name", data={"title": "   "}, follow_redirects=True)
    assert "Bitte einen Namen angeben" in r.get_data(as_text=True) and _link(token).title == "Rentenberatung"
    # Buero-Admin darf jeden Link des Bueros umbenennen
    admin.post(f"/kalender/buchungslinks/{link.id}/name", data={"title": "Vom Admin"})
    assert _link(token).title == "Vom Admin"


def test_delete_hides_link_invalidates_url_keeps_appointments(app, setup):
    _admin, token = _open_link(app, setup)
    laura = login(app, "laura@example.com")
    link = _link(token)
    customer = app.test_client()
    assert _book(customer, token, _slots(customer, token)[0], "Kundin Eins").status_code == 303
    assert laura.post(f"/kalender/buchungslinks/{link.id}/loeschen").status_code == 302
    html = laura.get("/kalender/buchungslinks").get_data(as_text=True)
    assert f'data-bl-row="{link.id}"' not in html
    r = app.test_client().get(f"/termin/buchen/{token}")
    assert r.status_code == 404 and "nicht gültig" in r.get_data(as_text=True)
    page_post = app.test_client().post(f"/termin/buchen/{token}", data={"slot": "x", "submission": "abcdefghij", "name": "X Y", "phone": "0171 1234567"})
    assert page_post.status_code == 404
    with bypass_tenant_scope():
        assert CalendarEvent.query.filter(CalendarEvent.title == "Beratungsgespräch – Kundin Eins", CalendarEvent.status == "scheduled").count() == 1
        assert BookingLink.query.filter(BookingLink.id == link.id).one().deleted_at is not None
    # geloeschter Link: weder umbenennen noch erneut loeschen
    assert laura.post(f"/kalender/buchungslinks/{link.id}/name", data={"title": "Neu"}).status_code == 404
    assert laura.post(f"/kalender/buchungslinks/{link.id}/loeschen").status_code == 404
    # ein neuer Link funktioniert unabhaengig davon
    _a, token2 = _open_link(app, setup)
    assert app.test_client().get(f"/termin/buchen/{token2}").status_code == 200


def test_proposal_link_rename_and_delete_by_admin(app, setup):
    admin, token, day = _create(app, setup, customer=False)
    link = _link(token)
    admin.post(f"/kalender/buchungslinks/{link.id}/name", data={"title": "Jahresgespräch"})
    assert "Jahresgespräch" in app.test_client().get(f"/termin/buchen/{token}").get_data(as_text=True)
    admin.post(f"/kalender/buchungslinks/{link.id}/loeschen")
    assert app.test_client().get(f"/termin/buchen/{token}").status_code == 404


def test_permissions_rename_delete(app, setup):
    admin, token, _day = _create(app, setup)  # Link des Admins
    link = _link(token)
    # Mitarbeiterin ohne Freigabe
    laura = login(app, "laura@example.com")
    assert laura.post(f"/kalender/buchungslinks/{link.id}/name", data={"title": "x"}).status_code == 403
    assert laura.post(f"/kalender/buchungslinks/{link.id}/loeschen").status_code == 403
    # mit Freigabe, aber fremder Link
    admin.post(f"/mitarbeiter/{setup.laura_id}/buchungslinks", data={"allowed": "1"})
    assert laura.post(f"/kalender/buchungslinks/{link.id}/name", data={"title": "x"}).status_code == 403
    assert laura.post(f"/kalender/buchungslinks/{link.id}/loeschen").status_code == 403
    assert _link(token).title == "Beratungsgespräch" and _link(token).deleted_at is None
    # anderes Buero
    other = login(app, "admin-b@example.com")
    assert other.post(f"/kalender/buchungslinks/{link.id}/loeschen").status_code == 404
    assert _link(token).deleted_at is None
    assert re.search(r"Beratungsgespräch", app.test_client().get(f"/termin/buchen/{token}").get_data(as_text=True))
