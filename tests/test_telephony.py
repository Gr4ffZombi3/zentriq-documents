"""Anruferkennung ueber Webex Calling - durchgespielt mit fiktiven Anrufdaten im Format der Cisco-Doku.

Geprueft: Signatur (X-Webex-Signature SHA-256/512, X-Spark-Signature SHA1), doppelte Meldungen nur
einmal, Zuordnung Durchwahl -> Benutzer, Suche nur im eigenen Buero mit 0228.../+49 228...,
die vier Anzeigen (ein Treffer, mehrere, keiner, unterdrueckt), Ende-Meldung, Rechte der
Browser-Routen, persoenlicher Live-Kanal und keine Rufnummern in Logs."""

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import uuid

import pytest

from app.models import Customer, EmployeeProfile, TelephonyCall, Tenant, User, UserRole
from app.services import telephony
from app.telephony_cli import fake_payload
from app.tenancy import bypass_tenant_scope, set_current_tenant_id
from live_service import server, tokens

PASSWORD = "geheimespasswort123"
SECRET = "webhook-test-secret"
PERSON_UUID = "96b089d9-0fd8-4e74-9b76-50daf768705c"
PERSON_ID = base64.b64encode(f"ciscospark://us/PEOPLE/{PERSON_UUID}".encode()).decode().rstrip("=")
URL = "/telefonie/webex/webhook"


def _user(db, tenant, email, role=UserRole.OFFICE_ADMIN, name=None):
    user = User(tenant_id=tenant.id, email=email, role=role, is_active=True)
    user.set_password(PASSWORD)
    db.session.add(user)
    db.session.flush()
    if name:
        db.session.add(EmployeeProfile(tenant_id=tenant.id, user_id=user.id, display_name=name))
    db.session.commit()
    return user


def _login(app, user):
    client = app.test_client()
    response = client.post("/auth/login", data={"login_type": "email", "identifier": user.email, "password": PASSWORD})
    assert response.status_code == 302
    return client


def _post(client, payload, *, secret=SECRET, header="webex", body=None):
    raw = body if body is not None else json.dumps(payload).encode()
    headers = {}
    if header == "webex":
        headers["X-Webex-Signature"] = "SHA-256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    elif header == "webex512":
        headers["X-Webex-Signature"] = (
            "SHA-256=" + "0" * 64 + ", SHA-512=" + hmac.new(secret.encode(), raw, hashlib.sha512).hexdigest()
        )
    elif header == "spark":
        headers["X-Spark-Signature"] = hmac.new(secret.encode(), raw, hashlib.sha1).hexdigest()
    return client.post(URL, data=raw, content_type="application/json", headers=headers)


def _ring(call_id=None, number="02281234567", privacy=False, person=PERSON_ID):
    return fake_payload(person, call_id or f"call-{uuid.uuid4().hex}", state="alerting", number=number, privacy=privacy, event="created")


def _sent(app):
    sent = app.extensions.setdefault("zq_live_sent", [])
    return sent


@pytest.fixture()
def office(app, db, tenant):
    app.config["WEBEX_WEBHOOK_SECRET"] = SECRET
    laura = _user(db, tenant, "laura@example.com", name="Laura Beispiel")
    mia = _user(db, tenant, "mia@example.com", UserRole.EMPLOYEE, name="Mia Muster")
    max_m = Customer(tenant_id=tenant.id, name="Max Mustermann", phone_landline="+49 228 1234567", city="Bonn", postal_code="53111")
    db.session.add(max_m)
    db.session.commit()
    telephony.assign_line(laura, PERSON_ID, "Durchwahl 21")
    set_current_tenant_id(tenant.id)
    _sent(app).clear()  # Meldungen der Kundenanlage
    return {"laura": laura, "mia": mia, "max": max_m, "tenant": tenant}


# --- Kennungen, Nummern, Signatur ----------------------------------------------------------------


def test_person_uuid_accepts_api_id_and_uuid():
    assert telephony.person_uuid(PERSON_ID) == PERSON_UUID
    assert telephony.person_uuid(PERSON_UUID.upper()) == PERSON_UUID
    assert telephony.person_uuid("kein-gueltiger-wert") is None
    assert telephony.person_uuid("") is None


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("0228 1234567", "+492281234567"),
        ("+49 228 1234567", "+492281234567"),
        ("tel:+492281234567", "+492281234567"),
        ("sip:02281234567@example.webex.com", "+492281234567"),
        ("0049 (0)228 1234567", "+492281234567"),
    ],
)
def test_number_spellings_match_customer_keys(raw, expected):
    assert telephony.number_keys(telephony.clean_number(raw)) == [expected]


def test_hidden_number_texts_are_not_numbers():
    assert telephony.clean_number("anonymous") is None
    assert telephony.clean_number("") is None
    assert telephony.clean_number(None) is None


def test_signature_variants():
    body = b'{"a":1}'
    sha256 = hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
    sha1 = hmac.new(SECRET.encode(), body, hashlib.sha1).hexdigest()
    assert telephony.verify_signature(SECRET, body, {"X-Webex-Signature": f"SHA-256={sha256}"})
    assert telephony.verify_signature(SECRET, body, {"X-Spark-Signature": sha1})
    assert not telephony.verify_signature(SECRET, body + b" ", {"X-Webex-Signature": f"SHA-256={sha256}"})
    assert not telephony.verify_signature("anderes", body, {"X-Spark-Signature": sha1})
    # Ganzer Header ist kein Digest; ohne Header nie gueltig; ohne Secret nie gueltig.
    assert not telephony.verify_signature(SECRET, body, {"X-Webex-Signature": sha256})
    assert not telephony.verify_signature(SECRET, body, {})
    assert not telephony.verify_signature("", body, {"X-Spark-Signature": sha1})


# --- Webhook ---------------------------------------------------------------------------------------


def test_webhook_not_configured_without_secret(app, client):
    app.config["WEBEX_WEBHOOK_SECRET"] = ""
    assert _post(client, _ring()).status_code == 503


def test_webhook_rejects_bad_signature(app, client, office):
    assert _post(client, _ring(), secret="falsch").status_code == 401
    assert _post(client, _ring(), header=None).status_code == 401
    assert TelephonyCall.query.count() == 0


@pytest.mark.parametrize("header", ["webex", "webex512", "spark"])
def test_ringing_creates_one_call_and_notifies_only_the_assigned_user(app, client, office, header):
    response = _post(client, _ring(call_id="call-1"), header=header)
    assert response.status_code == 200 and response.get_json()["result"] == "ringing"
    call = TelephonyCall.query.one()
    assert call.user_id == office["laura"].id and call.status == "ringing"
    assert _sent(app) == [(f"zq:live:{office['tenant'].id}:user:{office['laura'].id}", {"type": "call", "id": call.id})]

    # Dieselbe Meldung erneut (Webex stellt doppelt zu): kein zweiter Anruf, kein zweiter Hinweis.
    again = _post(client, _ring(call_id="call-1"), header=header)
    assert again.status_code == 200 and again.get_json()["result"] == "duplicate"
    assert TelephonyCall.query.count() == 1
    assert len(_sent(app)) == 1


def test_end_events_hide_once(app, client, office):
    _post(client, _ring(call_id="call-2"))
    payload = fake_payload(PERSON_ID, "call-2", state="disconnected", number=None, privacy=False, event="deleted")
    assert _post(client, payload).get_json()["result"] == "ended"
    assert _post(client, payload).get_json()["result"] == "duplicate"
    call = TelephonyCall.query.one()
    assert call.status == "ended" and call.ended_at is not None
    assert [m for _, m in _sent(app)] == [{"type": "call", "id": call.id}, {"type": "call_end", "id": call.id}]
    # Spaete Klingel-Meldung nach dem Auflegen: kein neuer Hinweis.
    assert _post(client, _ring(call_id="call-2")).get_json()["result"] == "duplicate"


def test_answered_hides_hint(app, client, office):
    _post(client, _ring(call_id="call-3"))
    answered = fake_payload(PERSON_ID, "call-3", state="connected", number=None, privacy=False, event="updated")
    assert _post(client, answered).get_json()["result"] == "answered"


def test_ignored_events_are_acknowledged(app, client, office):
    other = base64.b64encode(f"ciscospark://us/PEOPLE/{uuid.uuid4()}".encode()).decode()
    assert _post(client, _ring(person=other)).get_json()["result"] == "ignored:line"
    outgoing = _ring()
    outgoing["data"]["personality"] = "originator"
    assert _post(client, outgoing).get_json()["result"] == "ignored:outgoing"
    messages = _ring()
    messages["resource"] = "messages"
    assert _post(client, messages).get_json()["result"] == "ignored:resource"
    stale = _ring()
    stale["data"]["eventTimestamp"] = "2020-01-01T00:00:00.000Z"
    assert _post(client, stale).get_json()["result"] == "ignored:stale"
    assert _post(client, None, body=b"{kaputt").status_code == 400
    assert TelephonyCall.query.count() == 0
    assert _sent(app) == []


def test_org_filter(app, client, office):
    app.config["WEBEX_ORG_ID"] = "org-richtig"
    payload = _ring()
    payload["orgId"] = "org-falsch"
    assert _post(client, payload).get_json()["result"] == "ignored:org"
    payload = _ring()
    payload["orgId"] = "org-richtig"
    assert _post(client, payload).get_json()["result"] == "ringing"


def test_deactivated_user_gets_no_hint(app, client, db, office):
    office["laura"].is_active = False
    db.session.commit()
    assert _post(client, _ring()).get_json()["result"] == "ignored:line"


def test_logs_contain_no_number_or_secret(app, client, office, caplog):
    caplog.set_level(logging.DEBUG)
    _post(client, _ring(number="+49 228 7654321"))
    _post(client, _ring(number="+49 228 7654321"), secret="falsch")
    text = caplog.text
    for secret_part in ("7654321", "228 76", SECRET, PERSON_ID, PERSON_UUID):
        assert secret_part not in text


# --- Anzeige im Browser ---------------------------------------------------------------------------


def _detail(app, user, call_id):
    return _login(app, user).get(f"/arbeitsplatz/telefon/anrufe/{call_id}").get_json()["call"]


def _ring_and_get(app, client, number="0228 1234567", privacy=False):
    _post(client, _ring(number=number, privacy=privacy))
    return TelephonyCall.query.order_by(TelephonyCall.id.desc()).first()


def test_single_match_shows_name_and_opens_file_in_panel(app, client, office):
    # Gespeichert als "+49 228 1234567", Anruf meldet "0228 1234567".
    call = _ring_and_get(app, client, "0228 1234567")
    data = _detail(app, office["laura"], call.id)
    assert data["kind"] == "single"
    assert data["title"] == "Max Mustermann ruft an"
    assert data["customers"][0]["url"] == f"/customers/{office['max'].id}"
    assert data["customers"][0]["panel"] is True  # Seitenfenster: geoeffnete Akte bleibt


def test_international_spelling_matches_too(app, client, db, office):
    office["max"].phone_landline = "0228 1234567"
    db.session.commit()
    call = _ring_and_get(app, client, "+492281234567")
    assert _detail(app, office["laura"], call.id)["kind"] == "single"


def test_multiple_matches_offer_choice(app, client, db, office, tenant):
    db.session.add(Customer(tenant_id=tenant.id, name="Erika Mustermann", phone_private="0228/123 45 67"))
    db.session.commit()
    call = _ring_and_get(app, client)
    data = _detail(app, office["laura"], call.id)
    assert data["kind"] == "multiple"
    assert [c["name"] for c in data["customers"]] == ["Erika Mustermann", "Max Mustermann"]


def test_no_match_shows_number_and_callback_note(app, client, office):
    call = _ring_and_get(app, client, "+49 221 9999999")
    data = _detail(app, office["laura"], call.id)
    assert data["kind"] == "none"
    assert data["number"] == "02219999999"  # Anzeige wie an der Kundenakte (format_phone)
    client_laura = _login(app, office["laura"])
    page = client_laura.get(data["callback_url"])
    assert page.status_code == 200
    html = page.get_data(as_text=True)
    assert "Rückruf erbeten – Anruf von 02219999999" in html
    assert "9999999" not in data["callback_url"]  # Nummer nicht in URL/Access-Log


def test_suppressed_number_shows_unknown_caller(app, client, office):
    call = _ring_and_get(app, client, privacy=True)
    data = _detail(app, office["laura"], call.id)
    assert data == {**data, "kind": "unknown", "title": "Unbekannter Anrufer"}
    assert "number" not in data


def test_search_stays_in_own_office(app, client, db, office):
    other = Tenant(name="Anderes Büro", slug="anderes-buero")
    db.session.add(other)
    db.session.commit()
    set_current_tenant_id(other.id)
    db.session.add(Customer(tenant_id=other.id, name="Fremder Kunde", phone="+49 221 5555555"))
    db.session.commit()
    set_current_tenant_id(office["tenant"].id)
    call = _ring_and_get(app, client, "0221 5555555")
    data = _detail(app, office["laura"], call.id)
    assert data["kind"] == "none"


def test_only_own_calls_are_visible(app, client, office):
    call = _ring_and_get(app, client)
    mia = _login(app, office["mia"])
    assert mia.get(f"/arbeitsplatz/telefon/anrufe/{call.id}").status_code == 404
    assert mia.get(f"/arbeitsplatz/telefon/anrufe/{call.id}/rueckrufnotiz").status_code == 404
    assert mia.get("/arbeitsplatz/telefon/anrufe/aktuell").get_json() == {"call": None}
    assert mia.get("/arbeitsplatz/telefon/token").get_json() == {"enabled": False}


def test_employee_link_goes_to_assistant_card(app, client, db, office):
    telephony.assign_line(office["mia"], str(uuid.uuid4()), "Durchwahl 22")
    set_current_tenant_id(office["tenant"].id)
    with bypass_tenant_scope():
        line = telephony.TelephonyLine.query.filter_by(user_id=office["mia"].id).one()
        mia_person = base64.b64encode(f"ciscospark://us/PEOPLE/{line.webex_person_uuid}".encode()).decode()
    _post(client, _ring(person=mia_person))
    call = TelephonyCall.query.filter_by(user_id=office["mia"].id).one()
    data = _detail(app, office["mia"], call.id)
    assert data["customers"][0]["url"] == f"/zentriq?kunde={office['max'].id}"
    assert data["customers"][0]["panel"] is False


def test_current_call_after_page_reload(app, client, office):
    call = _ring_and_get(app, client)
    laura = _login(app, office["laura"])
    assert laura.get("/arbeitsplatz/telefon/anrufe/aktuell").get_json()["call"]["id"] == call.id
    _post(client, fake_payload(PERSON_ID, "x", state="disconnected", number=None, privacy=False, event="deleted"))
    call.status = "ended"
    from app.extensions import db

    db.session.commit()
    assert laura.get("/arbeitsplatz/telefon/anrufe/aktuell").get_json() == {"call": None}


def test_page_includes_call_alert_only_when_configured(app, office):
    laura = _login(app, office["laura"])
    assert "js/call-alert.js" in laura.get("/arbeitsplatz/neuigkeiten").get_data(as_text=True)
    app.config["WEBEX_WEBHOOK_SECRET"] = ""
    assert "js/call-alert.js" not in laura.get("/arbeitsplatz/neuigkeiten").get_data(as_text=True)


# --- Persoenlicher Live-Kanal -------------------------------------------------------------------------


def test_token_for_personal_channel(app, office):
    laura = _login(app, office["laura"])
    data = laura.get("/arbeitsplatz/telefon/token").get_json()
    assert data["enabled"] is True
    claims = tokens.verify(app.config["SECRET_KEY"], data["token"])
    assert claims["c"] == f"user:{office['laura'].id}" and claims["t"] == office["tenant"].id


def test_live_service_forwards_call_events_without_presence():
    class Broker:
        def __init__(self):
            self.published, self.hashes = [], {}

        async def publish(self, channel, data):
            self.published.append((channel, data))

        async def hset(self, key, field, value):
            self.hashes.setdefault(key, {})[field] = value

        async def hdel(self, key, *fields):
            pass

        async def hgetall(self, key):
            return self.hashes.get(key, {})

        async def expire(self, key, seconds):
            pass

    class Writer:
        def __init__(self):
            self.data = b""

        def write(self, data):
            self.data += data

        async def drain(self):
            pass

    broker = Broker()
    hub = server.Hub(broker, SECRET)
    conn = server.Connection({"u": 3, "t": 7, "n": "Laura", "c": "user:3"})
    writer = Writer()

    async def run():
        await hub._presence_entry(conn)
        await hub._announce(conn)
        await hub._forward(conn, writer, json.dumps({"type": "call", "id": 5}))
        await hub._forward(conn, writer, json.dumps({"type": "call_end", "id": 5}))

    asyncio.run(run())
    assert broker.published == [] and broker.hashes == {}
    assert b'event: call\ndata: {"id":5}' in writer.data
    assert b"event: call_end" in writer.data
