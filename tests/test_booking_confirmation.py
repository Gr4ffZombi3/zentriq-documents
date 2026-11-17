# ruff: noqa: F811  (Fixtures werden aus anderen Testmodulen importiert)
"""Bestaetigungs-E-Mail nach Buchung ueber oeffentliche Buchungslinks: genau eine je Buchung, an die
eigene Adresse, mit Buero als Absendername und Antwortadresse; Fehler lassen den Termin bestehen."""

import re
import smtplib
from email import message_from_string
from email.header import decode_header, make_header

import pytest

from app.extensions import db as _db
from app.models import BookingConfirmation, BookingLink, CalendarEvent, SystemErrorEvent
from app.models.office_settings import OfficeSettings
from app.services import booking_confirmation as bc
from app.services import booking_links as bl
from app.services.timetracking.clock import to_local
from app.tenancy import bypass_tenant_scope
from tests.test_booking_links import _csrf, _open_link, _slots, no_ai, setup  # noqa: F401
from tests.test_customer_notes import office  # noqa: F401
from tests.test_permissions_security import login, world  # noqa: F401


class FakeSMTP:
    """Ersetzt smtplib.SMTP: sammelt gesendete Nachrichten, kann einen Fehler simulieren."""

    sent: list = []
    fail = False

    def __init__(self, host, port, timeout=None):
        self.host = host

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def starttls(self, context=None):
        pass

    def login(self, user, password):
        pass

    def send_message(self, message):
        if FakeSMTP.fail:
            raise smtplib.SMTPRecipientsRefused({message["To"]: (550, b"nope")})
        FakeSMTP.sent.append(message_from_string(message.as_string()))


@pytest.fixture()
def smtp(app, monkeypatch, setup):
    FakeSMTP.sent, FakeSMTP.fail = [], False
    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    app.config.update(SMTP_HOST="smtp.ionos.test", SMTP_PORT=587, SMTP_USE_SSL=False, SMTP_USE_STARTTLS=True,
                      MAIL_FROM="Zentriq <termine@zentriq.test>", PUBLIC_URL="https://zentriq.test")
    with bypass_tenant_scope():
        s = OfficeSettings.query.filter_by(tenant_id=setup.tenant_a.id).one()
        s.email, s.phone, s.address = "kontakt@agentur-test.example", "030 1234567", "Musterstraße 1, 10115 Berlin"
        _db.session.commit()
    return FakeSMTP


def _hdr(msg, name):
    return str(make_header(decode_header(msg[name])))


def _body(msg):
    return msg.get_payload(decode=True).decode("utf-8")


def _book(client, token, slot, name, email, page=None, phone="0171 1234567"):
    page = page or client.get(f"/termin/buchen/{token}").get_data(as_text=True)
    return client.post(f"/termin/buchen/{token}", data={"slot": slot, "submission": _csrf(page), "name": name, "phone": phone, "email": email})


def test_two_bookings_same_link_each_address_gets_exactly_its_own(app, setup, smtp):
    _admin, token = _open_link(app, setup)
    first, second = app.test_client(), app.test_client()
    slots = _slots(first, token)
    r1 = _book(first, token, slots[0], "Kundin Eins", "eins@kunde.example")
    assert r1.status_code == 303
    confirmed = first.get(r1.headers["Location"]).get_data(as_text=True)
    assert "eins@kunde.example" in confirmed and "Bestätigung" in confirmed
    r2 = _book(second, token, slots[-1], "Kunde Zwei", "zwei@kunde.example")
    assert r2.status_code == 303

    assert [m["To"] for m in smtp.sent] == ["eins@kunde.example", "zwei@kunde.example"]
    one, two = smtp.sent
    with bypass_tenant_scope():
        link = BookingLink.query.filter(BookingLink.token_hash == bl.token_hash(token)).one()
        events = CalendarEvent.query.filter(CalendarEvent.client_ref.like(f"blo-{link.id}-%")).order_by(CalendarEvent.starts_at).all()
        assert bl.status(link) == "active" and link.booking_count == 2  # Link bleibt nutzbar
    for msg, event, name, other in ((one, events[0], "Kundin Eins", "Kunde Zwei"), (two, events[1], "Kunde Zwei", "Kundin Eins")):
        body = _body(msg)
        start = to_local(event.starts_at)
        assert f"Guten Tag {name}," in body and other not in body  # nur die eigene Buchung
        assert start.strftime("%d.%m.%Y") in body and start.strftime("%H:%M") in body
        assert start.strftime("%d.%m.%Y") in _hdr(msg, "Subject") and "Agentur Test" in _hdr(msg, "Subject")
        assert "Dauer:    30 Minuten" in body and "Beratungsgespräch" in body
        assert "Agentur Test" in body and "Musterstraße 1, 10115 Berlin" in body
        assert _hdr(msg, "From") == "Agentur Test <termine@zentriq.test>"  # Buero als Name, Adresse vom IONOS-Zugang
        assert msg["Reply-To"] == "kontakt@agentur-test.example"
        assert "Gebucht über" not in body and "Kontakt:" not in body  # keine internen Notizen
    with bypass_tenant_scope():
        rows = BookingConfirmation.query.order_by(BookingConfirmation.id).all()
        assert [(r.recipient, r.status, r.attempts) for r in rows] == [("eins@kunde.example", "sent", 1), ("zwei@kunde.example", "sent", 1)]
        assert [r.event_id for r in rows] == [e.id for e in events]


def test_double_click_and_reload_send_only_one(app, setup, smtp):
    _admin, token = _open_link(app, setup)
    client = app.test_client()
    page = client.get(f"/termin/buchen/{token}").get_data(as_text=True)
    slot = _slots(client, token)[0]
    for _ in range(3):  # Doppelklick / Antwort verloren: dieselbe Formularkennung
        assert _book(client, token, slot, "Kundin Eins", "eins@kunde.example", page=page).status_code == 303
    r = client.get(f"/termin/buchen/{token}?gebucht=1")
    assert "Ihr Termin ist gebucht" in r.get_data(as_text=True)
    client.get(f"/termin/buchen/{token}?gebucht=1")  # Neuladen der Bestaetigung
    assert len(smtp.sent) == 1
    with bypass_tenant_scope():
        row = BookingConfirmation.query.one()
        # Aufgabe ein zweites Mal ausgefuehrt (z. B. doppelt eingeplant): kein zweiter Versand
        assert bc.deliver(row.id) == {"sent": False, "reason": "already_handled"}
        assert bc.queue(row.tenant_id, row.event_id, {"email": "eins@kunde.example"}, "x") is None
    assert len(smtp.sent) == 1


def test_proposal_link_and_office_page_also_confirm(app, setup, smtp):
    from tests.test_booking_links import _create, _slot

    _admin, token, day = _create(app, setup, customer=False)
    client = app.test_client()
    r = _book(client, token, _slot(day, "10:00"), "Kundin Drei", "drei@kunde.example", phone="")
    assert r.status_code == 303
    assert len(smtp.sent) == 1
    body = _body(smtp.sent[0])
    # Terminart Telefon ohne Nummer: Hinweis statt Ort
    assert "Telefontermin" in body and "Telefonnummer" in body and "Ort:" not in body
    client.post(f"/termin/buchen/{token}", data={"slot": _slot(day, "10:00"), "name": "Kundin Drei", "email": "drei@kunde.example"})
    assert len(smtp.sent) == 1


def test_without_email_no_mail_and_invalid_email_rejected(app, setup, smtp):
    _admin, token = _open_link(app, setup)
    client = app.test_client()
    slot = _slots(client, token)[0]
    r = _book(client, token, slot, "Kundin Eins", "kein-at-zeichen")
    assert r.status_code == 409 and "gültige E-Mail" in r.get_data(as_text=True)
    r = _book(client, token, slot, "Kundin Eins", "eins@kunde.example\nBcc: x@y.example")
    assert r.status_code == 409
    assert _book(client, token, slot, "Kundin Eins", "").status_code == 303  # nur Telefon
    assert smtp.sent == []
    with bypass_tenant_scope():
        assert BookingConfirmation.query.count() == 0


def test_smtp_failure_keeps_booking_and_is_visible_and_resendable(app, setup, smtp):
    admin, token = _open_link(app, setup)
    smtp.fail = True
    client = app.test_client()
    slots = _slots(client, token)
    r = _book(client, token, slots[0], "Kundin Eins", "eins@kunde.example")
    assert r.status_code == 303 and "Ihr Termin ist gebucht" in client.get(r.headers["Location"]).get_data(as_text=True)
    with bypass_tenant_scope():
        assert CalendarEvent.query.filter(CalendarEvent.title == "Beratungsgespräch – Kundin Eins").count() == 1
        row = BookingConfirmation.query.one()
        assert row.status == "failed" and "SMTPRecipientsRefused" in row.error
        assert SystemErrorEvent.query.filter(SystemErrorEvent.location == "Buchungsbestätigung").count() == 1
    # Link bleibt nutzbar
    assert _book(app.test_client(), token, slots[-1], "Kunde Zwei", "").status_code == 303
    # Buero-Admin sieht den Fehler; Mitarbeiterin mit Linkfreigabe nicht
    html = admin.get("/kalender/buchungslinks").get_data(as_text=True)
    assert "Bestätigungs-E-Mails nicht zugestellt" in html and "eins@kunde.example" in html
    assert "Adresse vom Mailserver abgelehnt" in html and "SMTPRecipientsRefused" not in html
    assert "nicht zugestellt" not in login(app, "laura@example.com").get("/kalender/buchungslinks").get_data(as_text=True)
    assert login(app, "laura@example.com").post(f"/kalender/buchungslinks/bestaetigung/{row.id}/erneut").status_code == 403
    # Erneut senden klappt genau einmal
    smtp.fail = False
    html = admin.get("/kalender/buchungslinks").get_data(as_text=True)
    csrf = re.search(r'name="csrf_token" value="([^"]+)"', html)
    data = {"csrf_token": csrf.group(1)} if csrf else {}
    admin.post(f"/kalender/buchungslinks/bestaetigung/{row.id}/erneut", data=data)
    admin.post(f"/kalender/buchungslinks/bestaetigung/{row.id}/erneut", data=data)
    assert [m["To"] for m in smtp.sent] == ["eins@kunde.example"]
    with bypass_tenant_scope():
        assert _db.session.get(BookingConfirmation, row.id).status == "sent"
    assert "nicht zugestellt" not in admin.get("/kalender/buchungslinks").get_data(as_text=True)


def test_mail_not_configured_keeps_booking(app, setup, smtp):
    app.config.update(SMTP_HOST=None)
    _admin, token = _open_link(app, setup)
    client = app.test_client()
    assert _book(client, token, _slots(client, token)[0], "Kundin Eins", "eins@kunde.example").status_code == 303
    with bypass_tenant_scope():
        row = BookingConfirmation.query.one()
        assert row.status == "failed" and row.error == "not_configured"


def test_office_page_booking_confirms_once(app, setup, smtp):
    admin = login(app, "admin-a@example.com")
    r = admin.post("/kalender/einstellungen/online-buchung", data={"enabled": "1", "slug": "agentur-test", "title": "Beratungstermin",
                                                                  "minutes": "45", "assignee_id": str(setup.laura_id), "horizon_days": "14"})
    assert r.status_code == 302
    client = app.test_client()
    page = client.get("/termin/agentur-test").get_data(as_text=True)
    slot = re.findall(r'name="slot" value="([^"]+)"', page)[0]
    data = {"slot": slot, "name": "Kunde Vier", "phone": "", "email": "vier@kunde.example", "submission": _csrf(page)}
    assert client.post("/termin/agentur-test", data=data).status_code == 303
    assert client.post("/termin/agentur-test", data=data).status_code == 303
    assert [m["To"] for m in smtp.sent] == ["vier@kunde.example"]
    body = _body(smtp.sent[0])
    assert "Dauer:    45 Minuten" in body and "Ort:      Musterstraße 1, 10115 Berlin" in body
