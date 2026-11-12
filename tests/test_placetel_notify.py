"""Anruferkennung ueber Placetel Notify ("Anruf an externe API melden") - fiktive Anrufe im Format der
Placetel-Doku (github.com/Placetel/call-control-notify-api): application/x-www-form-urlencoded,
Signatur X-PLACETEL-SIGNATURE (HMAC-SHA256 hex). Antwort ist reiner Text, kein Routing-XML."""

import hashlib
import hmac
import logging
import uuid
from urllib.parse import urlencode

import pytest

from app.models import Customer, TelephonyCall, Tenant
from app.services import telephony
from app.telephony_cli import placetel_form
from app.tenancy import bypass_tenant_scope, set_current_tenant_id
from tests.test_telephony import _detail, _login, _sent, _user

SECRET = "placetel-test-secret"
URL = "/telefonie/placetel/notify"
OFFICE_NUMBER = "0228 9876500"
SIP_LAURA = "777laura@fpbx.de"


def _post(client, form, *, secret=SECRET, body=None, signature=None):
    raw = body if body is not None else urlencode(form).encode()
    sig = signature if signature is not None else hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    return client.post(URL, data=raw, content_type="application/x-www-form-urlencoded", headers={"X-PLACETEL-SIGNATURE": sig})


def _call_id():
    return hashlib.sha256(uuid.uuid4().bytes).hexdigest()


@pytest.fixture()
def office(app, db, tenant):
    app.config["PLACETEL_NOTIFY_SECRET"] = SECRET
    laura = _user(db, tenant, "laura@example.com", name="Laura Beispiel")
    anna = _user(db, tenant, "anna@example.com", name="Anna Admin")
    max_m = Customer(tenant_id=tenant.id, name="Max Mustermann", phone_landline="+49 228 1234567", city="Bonn")
    db.session.add(max_m)
    db.session.commit()
    telephony.assign_placetel(laura, number=OFFICE_NUMBER, peer=SIP_LAURA, label="Durchwahl 21")
    set_current_tenant_id(tenant.id)
    _sent(app).clear()
    return {"laura": laura, "anna": anna, "max": max_m, "tenant": tenant}


def test_documented_signature_example():
    """Beispiel aus der Placetel-Doku: Secret THE_SECRET -> a16d4905..."""
    payload = (b"call_id=4a4cbb39578170aed9a2761a7bec8c7e704a541f52291ef603d6f5f152980c3c&direction=in"
               b"&event=CallAccepted&from=0123456789&to=0987654321")
    good = {"X-PLACETEL-SIGNATURE": "a16d490518344c33afa96158d115b48a26a250da6b8c6bc1d6dedb0a47d13eaa"}
    assert telephony.verify_placetel_signature("THE_SECRET", payload, good)
    assert not telephony.verify_placetel_signature("THE_SECRET", payload + b"x", good)
    assert not telephony.verify_placetel_signature("", payload, good)
    assert not telephony.verify_placetel_signature("THE_SECRET", payload, {})


def test_signature_and_configuration_are_required(app, client, office):
    form = placetel_form("IncomingCall", _call_id(), to=OFFICE_NUMBER, caller="0228 1234567")
    assert _post(client, form, secret="falsch").status_code == 401
    assert _post(client, form, signature="").status_code == 401
    app.config["PLACETEL_NOTIFY_SECRET"] = ""
    assert _post(client, form).status_code == 503
    assert TelephonyCall.query.count() == 0


def test_incoming_call_shows_hint_once_and_answer_is_plain_text(app, client, office):
    call_id = _call_id()
    form = placetel_form("IncomingCall", call_id, to=OFFICE_NUMBER, caller="0228 1234567")
    response = _post(client, form)
    assert response.status_code == 200 and response.mimetype == "text/plain" and response.get_data(as_text=True) == "OK"
    assert "<Response" not in response.get_data(as_text=True)  # nie Routing-XML: Sprachmenue bleibt
    assert _post(client, form).status_code == 200  # doppelt zugestellt
    call = TelephonyCall.query.one()
    assert call.user_id == office["laura"].id and call.status == "ringing"
    assert [m for _t, m in _sent(app) if m["type"] == "call"] == [{"type": "call", "id": call.id}]
    data = _detail(app, office["laura"], call.id)
    assert data["kind"] == "single" and data["title"] == "Max Mustermann ruft an"


@pytest.mark.parametrize("caller,kind,title", [
    ("+49 228 1234567", "single", "Max Mustermann ruft an"),
    ("0228 5555555", "none", None),
    ("anonymous", "unknown", "Unbekannter Anrufer"),
])
def test_display_variants(app, client, office, caller, kind, title):
    _post(client, placetel_form("IncomingCall", _call_id(), to=OFFICE_NUMBER, caller=caller))
    data = _detail(app, office["laura"], TelephonyCall.query.one().id)
    assert data["kind"] == kind
    if title:
        assert data["title"] == title


def test_accepted_by_mapped_sip_user_keeps_hint_until_hangup(app, client, office):
    telephony.assign_placetel(office["anna"], number=OFFICE_NUMBER, peer=None, label="Zentrale")
    call_id = _call_id()
    _post(client, placetel_form("IncomingCall", call_id, to=OFFICE_NUMBER, caller="0228 1234567"))
    assert TelephonyCall.query.filter_by(status="ringing").count() == 2  # Sammelnummer: beide sehen den Anruf
    _sent(app).clear()
    _post(client, placetel_form("CallAccepted", call_id, to=OFFICE_NUMBER, caller="0228 1234567", peer=SIP_LAURA))
    laura_call = TelephonyCall.query.filter_by(user_id=office["laura"].id).one()
    anna_call = TelephonyCall.query.filter_by(user_id=office["anna"].id).one()
    assert laura_call.status == "ringing" and anna_call.status == "answered"
    assert [m["id"] for _t, m in _sent(app) if m["type"] == "call_end"] == [anna_call.id]
    _post(client, placetel_form("HungUp", call_id, to=OFFICE_NUMBER, caller="0228 1234567", hangup="accepted"))
    assert TelephonyCall.query.filter_by(user_id=office["laura"].id).one().status == "ended"


def test_sip_only_mapping_shows_hint_when_answered_behind_ivr(app, client, office):
    """Sprachmenue vorgeschaltet, nur der SIP-Benutzer ist zugeordnet: Hinweis bei Annahme."""
    telephony.assign_placetel(office["laura"], number=None, peer=SIP_LAURA, label="nur SIP")
    call_id = _call_id()
    assert _post(client, placetel_form("IncomingCall", call_id, to=OFFICE_NUMBER, caller="0228 1234567")).status_code == 200
    assert TelephonyCall.query.count() == 0
    _post(client, placetel_form("CallAccepted", call_id, to=OFFICE_NUMBER, caller="0228 1234567", peer="777LAURA@fpbx.de"))
    call = TelephonyCall.query.one()
    assert call.user_id == office["laura"].id and call.status == "ringing"
    _post(client, placetel_form("CallAccepted", call_id, to=OFFICE_NUMBER, caller="0228 1234567", peer=SIP_LAURA))
    assert TelephonyCall.query.count() == 1  # Wiederholung: kein zweiter Hinweis


def test_missed_call_ends_hint_and_other_events_are_ignored(app, client, office):
    call_id = _call_id()
    _post(client, placetel_form("IncomingCall", call_id, to=OFFICE_NUMBER, caller="0228 1234567"))
    _post(client, placetel_form("HungUp", call_id, to=OFFICE_NUMBER, caller="0228 1234567", hangup="missed"))
    assert TelephonyCall.query.one().status == "ended"
    # andere Nummer, ausgehend, unbekannter Anruf: still quittiert
    assert _post(client, placetel_form("IncomingCall", _call_id(), to="0228 1111111", caller="0228 1234567")).status_code == 200
    out = {"event": "OutgoingCall", "from": "0228 9876500", "to": "0228 1234567", "call_id": _call_id(), "direction": "out", "peer": SIP_LAURA}
    assert _post(client, out).status_code == 200
    assert _post(client, placetel_form("HungUp", _call_id(), to=OFFICE_NUMBER, caller="x")).status_code == 200
    assert _post(client, {"event": "Unsinn", "call_id": "x"}).status_code == 400
    assert TelephonyCall.query.count() == 1


def test_number_formats_and_office_isolation(app, client, db, office):
    other = Tenant(name="Fremdbüro", slug="fremd")
    db.session.add(other)
    db.session.commit()
    bob = _user(db, other, "bob@example.com", name="Bob")
    with bypass_tenant_scope():
        db.session.add(Customer(tenant_id=other.id, name="Fremder Kunde", phone="0228 1234567"))
        db.session.commit()
    telephony.assign_placetel(bob, number="0221 7000000", peer=None, label="Fremd")
    # Placetel meldet "to" ohne Leerzeichen bzw. mit +49 - beides trifft die Zuordnung
    _post(client, placetel_form("IncomingCall", _call_id(), to="+492289876500", caller="02281234567"))
    call = TelephonyCall.query.one()
    assert call.user_id == office["laura"].id
    data = _detail(app, office["laura"], call.id)
    assert [c["name"] for c in data["customers"]] == ["Max Mustermann"]  # nur eigenes Buero
    assert _login(app, bob).get(f"/arbeitsplatz/telefon/anrufe/{call.id}").status_code == 404


def test_assignment_rules(app, office):
    with pytest.raises(telephony.LineError):
        telephony.assign_placetel(office["anna"], number=None, peer=SIP_LAURA, label="")  # SIP schon vergeben
    with pytest.raises(telephony.LineError):
        telephony.assign_placetel(office["anna"], number=None, peer=None, label="")
    with pytest.raises(telephony.LineError):
        telephony.assign_placetel(office["anna"], number="keine nummer", peer=None, label="")


def test_logs_contain_no_number_or_secret(app, client, office, caplog):
    caplog.set_level(logging.DEBUG)
    call_id = _call_id()
    _post(client, placetel_form("IncomingCall", call_id, to=OFFICE_NUMBER, caller="0228 7654321"))
    _post(client, placetel_form("IncomingCall", call_id, to=OFFICE_NUMBER, caller="0228 7654321"), secret="falsch")
    text = caplog.text
    for part in ("7654321", "9876500", SECRET, SIP_LAURA, call_id):
        assert part not in text


def test_page_includes_call_alert_with_placetel_only(app, office):
    app.config["LIVE_ENABLED"] = True
    app.config["WEBEX_WEBHOOK_SECRET"] = ""
    html = _login(app, office["laura"]).get("/zentriq").get_data(as_text=True)
    assert "data-zq-calls" in html
