"""Live-Zusammenarbeit: Tokens, Token-Route (Rechte je Buero/Rolle), Aenderungsmeldungen nach dem
Commit (nicht bei Rollback), "bearbeitet gerade", der SSE-Dienst selbst (Anwesenheit, Weiterleitung,
Abweisung ungueltiger Tokens) und das Einfuegen der nginx-Location."""

import asyncio
import importlib.util
import json
from pathlib import Path

import pytest

from app.models import Customer, CustomerNote, EmployeeProfile, Tenant, User, UserRole
from app.tenancy import set_current_tenant_id
from live_service import server, tokens

PASSWORD = "geheimespasswort123"
SECRET = "test-secret-fuer-live"


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


def _sent(app):
    return app.extensions.setdefault("zq_live_sent", [])


@pytest.fixture()
def office(db, tenant):
    laura = _user(db, tenant, "laura@example.com", name="Laura Beispiel")
    mia = _user(db, tenant, "mia@example.com", UserRole.EMPLOYEE, name="Mia Muster")
    customer = Customer(tenant_id=tenant.id, name="Karl Kunde", first_name="Karl", last_name="Kunde", assigned_user_id=mia.id)
    db.session.add(customer)
    db.session.commit()
    return {"laura": laura, "mia": mia, "customer": customer, "tenant": tenant}


# --- Tokens --------------------------------------------------------------------------------------


def test_token_roundtrip_and_rejections():
    token = tokens.issue(SECRET, user_id=3, tenant_id=7, name="Laura", channel="customer:42")
    assert tokens.verify(SECRET, token) == {"u": 3, "t": 7, "n": "Laura", "c": "customer:42"}
    assert tokens.verify("anderes-secret", token) is None
    assert tokens.verify(SECRET, token, max_age=-1) is None  # abgelaufen
    assert tokens.verify(SECRET, token + "x") is None
    assert tokens.verify(SECRET, "") is None
    with pytest.raises(ValueError):
        tokens.issue(SECRET, user_id=1, tenant_id=1, name="x", channel="customer:../1")
    assert tokens.redis_channel(7, "customer:42") == "zq:live:7:customer:42"


def test_token_route_respects_office_and_role(app, db, office):
    # Kunde eines anderen Bueros (vor dem Login anlegen - der Mandantenwechsel gilt prozessweit)
    other_tenant = Tenant(name="Anderes Büro", slug="anderes")
    db.session.add(other_tenant)
    db.session.commit()
    set_current_tenant_id(other_tenant.id)
    foreign = Customer(tenant_id=other_tenant.id, name="Fremder Kunde")
    db.session.add(foreign)
    db.session.commit()
    foreign_id = foreign.id
    set_current_tenant_id(office["tenant"].id)

    client = _login(app, office["mia"])
    data = client.get(f"/arbeitsplatz/live/kunde/{office['customer'].id}/token").get_json()
    assert data["enabled"] is True and data["stream"] == "/live/stream" and data["me"] == office["mia"].id
    claims = tokens.verify(app.config["SECRET_KEY"], data["token"])
    assert claims == {"u": office["mia"].id, "t": office["tenant"].id, "n": "Mia Muster", "c": f"customer:{office['customer'].id}"}

    # Kunde eines anderen Bueros: 404
    assert client.get(f"/arbeitsplatz/live/kunde/{foreign_id}/token").status_code == 404

    # Ohne Anmeldung: Weiterleitung, kein Token
    assert app.test_client().get(f"/arbeitsplatz/live/kunde/{office['customer'].id}/token").status_code in (302, 401)

    app.config["LIVE_ENABLED"] = False
    assert client.get(f"/arbeitsplatz/live/kunde/{office['customer'].id}/token").get_json() == {"enabled": False}


def test_name_never_email(app, db, office, tenant):
    nameless = _user(db, tenant, "ohne-name@example.com", UserRole.EMPLOYEE)
    client = _login(app, nameless)
    data = client.get(f"/arbeitsplatz/live/kunde/{office['customer'].id}/token").get_json()
    assert tokens.verify(app.config["SECRET_KEY"], data["token"])["n"] == "Ein Teammitglied"


# --- Aenderungsmeldungen ----------------------------------------------------------------------------


def test_commit_publishes_change_rollback_does_not(app, db, office):
    customer = office["customer"]
    _sent(app).clear()
    db.session.add(CustomerNote(tenant_id=customer.tenant_id, customer_id=customer.id, text="Rückruf erbeten"))
    db.session.rollback()
    assert _sent(app) == []

    db.session.add(CustomerNote(tenant_id=customer.tenant_id, customer_id=customer.id, text="Angebot besprochen"))
    db.session.commit()
    (target, message), = _sent(app)
    assert target == f"zq:live:{customer.tenant_id}:customer:{customer.id}"
    assert message["type"] == "changed" and message["kinds"] == ["Notiz"]
    assert message["by"] is None  # ausserhalb einer Anfrage (z. B. Celery): "wurde aktualisiert"
    assert "Angebot" not in json.dumps(message)  # keine Kundendaten in der Meldung


def test_unchanged_customer_publishes_nothing(app, db, office):
    customer = office["customer"]
    _sent(app).clear()
    customer.name = customer.name  # keine echte Aenderung
    db.session.commit()
    assert _sent(app) == []


def test_field_edit_via_api_names_the_editor(app, db, office):
    client = _login(app, office["laura"])
    customer = office["customer"]
    card = client.get(f"/zentriq/api/kunden/{customer.id}").get_json()["card"]
    _sent(app).clear()
    response = client.patch(f"/zentriq/api/kunden/{customer.id}", json={"version": card["edit"]["version"], "city": "Leipzig"})
    assert response.status_code == 200, response.get_json()
    changed = [m for _t, m in _sent(app) if m["type"] == "changed"]
    assert changed and changed[-1]["kinds"] == ["Stammdaten"]
    assert changed[-1]["by"] == {"u": office["laura"].id, "n": "Laura Beispiel"}

    # Veraltete Version: Konflikt statt stillem Ueberschreiben
    stale = client.patch(f"/zentriq/api/kunden/{customer.id}", json={"version": "2000-01-01T00:00:00", "city": "Halle"})
    assert stale.status_code == 409


def test_editor_name_known_even_if_profile_not_loaded(app, db, office):
    """Mitarbeiterin speichert ueber die Assistenten-Karte; ihr Profil ist in dieser Anfrage noch
    nicht geladen - der Name muss trotzdem im Hinweis stehen (nicht "wurde aktualisiert")."""
    client = _login(app, office["mia"])
    customer_id = office["customer"].id
    version = client.get(f"/zentriq/api/kunden/{customer_id}").get_json()["card"]["edit"]["version"]
    db.session.expire_all()
    _sent(app).clear()
    assert client.patch(f"/zentriq/api/kunden/{customer_id}", json={"version": version, "city": "Leipzig"}).status_code == 200
    changed = [m for _t, m in _sent(app) if m["type"] == "changed"]
    assert changed[-1]["by"] == {"u": office["mia"].id, "n": "Mia Muster"}


def test_editing_route(app, db, office):
    client = _login(app, office["mia"])
    _sent(app).clear()
    url = f"/arbeitsplatz/live/kunde/{office['customer'].id}/bearbeiten"
    assert client.post(url, json={"on": True, "part": "Stammdaten"}).get_json() == {"ok": True}
    (_target, message), = _sent(app)
    assert message == {"type": "editing", "by": {"u": office["mia"].id, "n": "Mia Muster"}, "on": True, "part": "Stammdaten"}


def test_detail_page_has_live_marker(app, db, office):
    client = _login(app, office["laura"])
    page = client.get(f"/customers/{office['customer'].id}").get_data(as_text=True)
    assert f'data-zq-live="/arbeitsplatz/live/kunde/{office["customer"].id}/token"' in page
    assert "js/live.js" in page
    panel = client.get(f"/customers/{office['customer'].id}?panel=1").get_data(as_text=True)
    assert "data-zq-live=" in panel


# --- SSE-Dienst ---------------------------------------------------------------------------------------


async def _open(port, token):
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(f"GET /live/stream?token={token} HTTP/1.1\r\nHost: x\r\n\r\n".encode())
    await writer.drain()
    return reader, writer


async def _event(reader, name, timeout=2.0):
    """Liest bis zum naechsten Ereignis `name` und liefert dessen Daten."""
    async def read():
        current = None
        while True:
            line = (await reader.readline()).decode().rstrip("\n")
            if line.startswith("event: "):
                current = line[7:]
            elif line.startswith("data: ") and current == name:
                return json.loads(line[6:])
    return await asyncio.wait_for(read(), timeout)


def test_sse_presence_forwarding_and_rejection():
    async def scenario():
        broker = server.MemoryBroker()
        hub = server.Hub(broker, SECRET)
        srv = await asyncio.start_server(hub.handle, "127.0.0.1", 0)
        port = srv.sockets[0].getsockname()[1]
        dispatcher = asyncio.create_task(hub.dispatch())
        try:
            # Gesundheitscheck und ungueltiges Token
            r, w = await asyncio.open_connection("127.0.0.1", port)
            w.write(b"GET /live/health HTTP/1.1\r\n\r\n")
            assert (await r.read()).startswith(b"HTTP/1.1 200")
            r, w = await _open(port, "kaputt")
            assert (await r.read()).startswith(b"HTTP/1.1 403")
            other_office = tokens.issue(SECRET, user_id=9, tenant_id=2, name="Fremd", channel="customer:5")

            laura = tokens.issue(SECRET, user_id=1, tenant_id=1, name="Laura", channel="customer:5")
            mia = tokens.issue(SECRET, user_id=2, tenant_id=1, name="Mia", channel="customer:5")
            r1, w1 = await _open(port, laura)
            assert (await r1.readline()).startswith(b"HTTP/1.1 200")
            assert await _event(r1, "presence") == {"viewers": [{"u": 1, "n": "Laura"}]}
            r2, w2 = await _open(port, mia)
            assert await _event(r1, "presence") == {"viewers": [{"u": 1, "n": "Laura"}, {"u": 2, "n": "Mia"}]}
            # Gleiche Kundennummer, anderes Buero: sieht niemanden aus Buero 1
            r3, w3 = await _open(port, other_office)
            assert await _event(r3, "presence") == {"viewers": [{"u": 9, "n": "Fremd"}]}

            await broker.publish("zq:live:1:customer:5", json.dumps({"type": "changed", "kinds": ["Notiz"], "by": {"u": 2, "n": "Mia"}}))
            assert await _event(r1, "changed") == {"kinds": ["Notiz"], "by": {"u": 2, "n": "Mia"}}
            await broker.publish("zq:live:1:customer:5", json.dumps({"type": "intern", "x": 1}))  # unbekannt: nicht weiter
            await broker.publish("zq:live:1:customer:5", json.dumps({"type": "editing", "by": {"u": 2, "n": "Mia"}, "on": True}))
            assert (await _event(r1, "editing"))["on"] is True

            w2.close()  # Mia schliesst die Akte
            assert await _event(r1, "presence") == {"viewers": [{"u": 1, "n": "Laura"}]}
            w1.close()
            w3.close()
        finally:
            dispatcher.cancel()
            srv.close()

    asyncio.run(scenario())


def test_serve_stops_quickly_with_open_stream():
    """SIGTERM (systemd restart) darf nicht an offenen SSE-Verbindungen haengen bleiben."""
    async def scenario():
        hub = server.Hub(server.MemoryBroker(), SECRET)
        stop_after = {}

        async def run():
            import socket

            with socket.socket() as s:
                s.bind(("127.0.0.1", 0))
                port = s.getsockname()[1]
            stop_after["port"] = port
            await server.serve(hub, "127.0.0.1", port)

        task = asyncio.create_task(run())
        await asyncio.sleep(0.2)
        token = tokens.issue(SECRET, user_id=1, tenant_id=1, name="Laura", channel="customer:5")
        reader, _writer = await _open(stop_after["port"], token)
        await _event(reader, "presence")
        import os
        import signal

        os.kill(os.getpid(), signal.SIGTERM)
        await asyncio.wait_for(task, timeout=4)

    asyncio.run(scenario())


def test_sse_stale_presence_is_dropped():
    async def scenario():
        clock = [1000.0]
        broker = server.MemoryBroker()
        hub = server.Hub(broker, SECRET, clock=lambda: clock[0])
        await broker.hset("zq:presence:1:customer:5", "alt", json.dumps({"u": 4, "n": "Weg", "ts": 900}))
        await broker.hset("zq:presence:1:customer:5", "neu", json.dumps({"u": 1, "n": "Da", "ts": 990}))
        assert await hub.viewers("zq:presence:1:customer:5") == [{"u": 1, "n": "Da"}]
        assert "alt" not in broker.hashes["zq:presence:1:customer:5"]

    asyncio.run(scenario())


def test_parse_request():
    assert server.parse_request(b"GET /live/stream?token=abc HTTP/1.1\r\n\r\n") == ("GET", "/live/stream", {"token": "abc"})
    assert server.parse_request(b"kaputt\r\n\r\n") is None


# --- nginx ----------------------------------------------------------------------------------------------


def test_nginx_live_location_insert_is_idempotent():
    root = Path(__file__).resolve().parent.parent / "deploy" / "nginx"
    spec = importlib.util.spec_from_file_location("live_location", root / "live_location.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    site = (
        "server {\n    listen 80;\n    return 301 https://$host$request_uri;\n}\n\n"
        "server {\n    listen 443 ssl;\n\n    location / {\n        proxy_pass http://127.0.0.1:8000;\n    }\n}\n"
    )
    once = module.add_live(site)
    assert once.count("location /live/ {") == 1
    assert "proxy_buffering off;" in once and "proxy_pass http://127.0.0.1:8001;" in once
    assert once.index("location /live/") < once.index("location / {")
    assert "\n\n\n" not in once
    assert module.add_live(once) == once
    template = (root / "zentriq.conf.template").read_text()
    assert "location /live/ {" in template and module.add_live(template) == template
