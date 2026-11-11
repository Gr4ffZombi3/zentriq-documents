# ruff: noqa: F811  (Fixture "world" wird aus test_permissions_security importiert)
"""Hybridbetrieb KI: eigener Rechner (Test-Worker ueber die echte HTTP-Schnittstelle).

Geprueft: Zugang (Hash, Widerruf), Bueroschranke, feste Auftragsarten, Lease/Neuvergabe ohne
doppelte Ergebnisse, Ablaufregel (Gemini fuer Einfaches, Rechner fuer Anhaenge/Lange/erzwungen,
kein Cloud-Rueckfall), Warten bei ausgeschaltetem Laptop, Kfz-Versicherungsschein ueber den
Rechner, Statusseite und Protokolle ohne Inhalte. Anbieter werden nie echt aufgerufen."""

import base64
import io
import json
import logging
from datetime import timedelta

import pytest

from app.extensions import db
from app.models.ai import AiMessage
from app.models.huk_quote import HukQuoteJob
from app.models.local_ai import LocalAiJob, LocalWorker, utcnow_naive
from app.services import local_ai
from app.services.ai import adapters, state
from app.tenancy import bypass_tenant_scope, use_tenant_id
from tests.test_kfz_vergleich import MISSING_INPUT, SCHEIN, _pdf
from tests.test_permissions_security import login, world  # noqa: F401

API = "/api/rechner"


class FakeProviders:
    def __init__(self):
        self.calls = []

    def __call__(self, provider, model_id, system, messages, max_tokens, timeout, tools=None):
        self.calls.append({"provider": provider, "model": model_id, "messages": list(messages)})
        return adapters.ProviderResult(f"Antwort von {provider}", 100, 50)


@pytest.fixture(autouse=True)
def _setup(app, monkeypatch, tmp_path):
    state.reset_memory()
    app.config["GEMINI_API_KEY"] = "gm-test"
    app.config["ZENTRIQ_AI_CACHE_TTL_SECONDS"] = 0
    app.config["HUK_STORAGE_DIR"] = str(tmp_path / "huk")
    app.config["HUK_BROWSER_TMP_DIR"] = str(tmp_path / "huk-tmp")
    yield
    state.reset_memory()


@pytest.fixture()
def fake(monkeypatch):
    providers = FakeProviders()
    monkeypatch.setattr(adapters, "call", providers)
    return providers


def _worker(app, client, name="Laptop"):
    """Zugang ueber die Oberflaeche anlegen (Buero-Admin); gibt den Schluessel zurueck."""
    html = client.post("/mein-rechner/zugang", data={"name": name}).get_data(as_text=True)
    token = html.split("zugang = ")[1].split("<")[0].strip()
    assert token.startswith("zqw_")
    return token


class FakeWorker:
    """Test-Worker: spricht die echte HTTP-Schnittstelle, ohne Modell."""

    def __init__(self, app, token):
        self.client = app.test_client()
        self.headers = {"Authorization": f"Bearer {token}"}

    def post(self, path, body=None):
        return self.client.post(API + path, json={"modell": "testmodell:1b", "version": "1.0", **(body or {})}, headers=self.headers)

    def claim(self, kinds=None):
        resp = self.post("/auftrag", {"arten": kinds} if kinds else {})
        return resp.get_json() if resp.status_code == 200 else None

    def finish(self, job, text):
        return self.post(f"/auftrag/{job['id']}/ergebnis", {"lease": job["lease"], "text": text})


def _send(client, conversation_id, content, **extra):
    resp = client.post(f"/zentriq/api/conversations/{conversation_id}/messages", json={"content": content, **extra})
    assert resp.status_code == 202, resp.get_data(as_text=True)
    reply_id = resp.get_json()["reply"]["id"]
    return reply_id, client.get(f"/zentriq/api/conversations/{conversation_id}/messages/{reply_id}").get_json()["message"]


def _conversation(client, model="auto"):
    return client.post("/zentriq/api/conversations", json={"model": model}).get_json()["conversation"]["id"]


# --- Zugang und Schnittstelle -----------------------------------------------------------------------


def test_access_is_hashed_revocable_and_office_bound(app, world):
    admin = login(app, "admin-a@example.com")
    token = _worker(app, admin)
    with bypass_tenant_scope():
        worker = LocalWorker.query.one()
        assert worker.token_hash == local_ai.hash_token(token) and token not in (worker.token_prefix + worker.token_hash)
    w = FakeWorker(app, token)
    assert w.post("/hallo").status_code == 200
    assert FakeWorker(app, "zqw_falsch").post("/hallo").status_code == 401
    assert app.test_client().post(API + "/hallo", json={}).status_code == 401
    # Mitarbeiter duerfen keine Zugaenge anlegen.
    assert login(app, "dennis@example.com").post("/mein-rechner/zugang", data={"name": "x"}).status_code == 403
    admin.post(f"/mein-rechner/zugang/{worker.id}/widerrufen")
    assert w.post("/hallo").status_code == 401


def test_worker_only_gets_jobs_of_its_office_and_allowed_kinds(app, world):
    token_a = _worker(app, login(app, "admin-a@example.com"))
    token_b = _worker(app, login(app, "admin-b@example.com"))
    with bypass_tenant_scope():
        office_a = LocalWorker.query.filter(LocalWorker.token_hash == local_ai.hash_token(token_a)).one().tenant_id
    with use_tenant_id(office_a):
        user = type("U", (), {"tenant_id": office_a, "id": None})()
        local_ai.enqueue(user, "assistant_chat", {"system": "", "messages": [{"role": "user", "content": "Hallo"}]})
    assert FakeWorker(app, token_b).claim() is None
    assert FakeWorker(app, token_a).claim(["unbekannt"]) is None
    job = FakeWorker(app, token_a).claim(["assistant_chat"])
    assert job and job["art"] == "assistant_chat" and job["eingabe"]["messages"][0]["content"] == "Hallo"


def test_lease_expiry_reassigns_without_duplicate_results(app, world):
    token = _worker(app, login(app, "admin-a@example.com"))
    with bypass_tenant_scope():
        tenant_id = LocalWorker.query.one().tenant_id
    with use_tenant_id(tenant_id):
        local_ai.enqueue(type("U", (), {"tenant_id": tenant_id, "id": None})(), "assistant_chat", {"messages": []})
    w = FakeWorker(app, token)
    first = w.claim()
    assert w.claim() is None  # laeuft, Lease gueltig
    with bypass_tenant_scope():
        LocalAiJob.query.one().lease_until = utcnow_naive() - timedelta(seconds=1)
        db.session.commit()
    second = w.claim()  # abgebrochener Lauf -> erneut vergeben
    assert second["id"] == first["id"] and second["lease"] != first["lease"]
    assert w.finish(first, "alt").status_code == 409  # veraltete Lease
    assert w.finish(second, "neu").get_json()["ergebnis"] == "ok"
    assert w.finish(second, "nochmal").get_json()["ergebnis"] == "duplicate"
    with bypass_tenant_scope():
        job = LocalAiJob.query.one()
        assert job.status == "done" and job.result["text"] == "neu" and job.payload is None and job.attempts == 2


def test_transient_errors_requeue_then_fail_after_max_attempts(app, world):
    app.config["LOCAL_AI_MAX_ATTEMPTS"] = 2
    token = _worker(app, login(app, "admin-a@example.com"))
    with bypass_tenant_scope():
        tenant_id = LocalWorker.query.one().tenant_id
    with use_tenant_id(tenant_id):
        local_ai.enqueue(type("U", (), {"tenant_id": tenant_id, "id": None})(), "assistant_chat", {"messages": []})
    w = FakeWorker(app, token)
    job = w.claim()
    w.post(f"/auftrag/{job['id']}/fehler", {"lease": job["lease"], "meldung": "Modell laedt", "wiederholen": True})
    with bypass_tenant_scope():
        assert LocalAiJob.query.one().status == "queued"
    job = w.claim()
    w.post(f"/auftrag/{job['id']}/fehler", {"lease": job["lease"], "meldung": "Modell laedt", "wiederholen": True})
    with bypass_tenant_scope():
        assert LocalAiJob.query.one().status == "failed"


# --- Ablaufregel im Assistenten ---------------------------------------------------------------------


def test_simple_requests_stay_with_gemini_even_with_worker(app, world, fake):
    admin = login(app, "admin-a@example.com")
    _worker(app, admin)
    conv = _conversation(admin)
    _reply_id, reply = _send(admin, conv, "Wie formuliere ich eine freundliche Erinnerung?")
    assert reply["status"] == "done" and fake.calls and fake.calls[0]["provider"] == "gemini"
    with bypass_tenant_scope():
        assert LocalAiJob.query.count() == 0


def test_long_input_goes_to_worker_and_waits_while_laptop_is_off(app, world, fake):
    admin = login(app, "admin-a@example.com")
    token = _worker(app, admin)
    conv = _conversation(admin)
    long_text = "Bitte analysiere diesen Vertragstext ausführlich. " + "Absatz mit Bedingungen. " * 80
    reply_id, reply = _send(admin, conv, long_text)
    assert reply["status"] == "pending" and reply["local"]["status"] == "queued"
    assert "nicht erreichbar" in reply["progress"]
    assert fake.calls == []  # kein Cloud-Modell
    # Laenger als die normale Frist: laeuft trotzdem nicht ab und blockiert das Gespraech nicht.
    with bypass_tenant_scope():
        msg = db.session.get(AiMessage, reply_id)
        msg.created_at = utcnow_naive() - timedelta(hours=3)
        db.session.commit()
    _other_id, other = _send(admin, conv, "Danke!")
    assert other["status"] == "done"
    # Laptop meldet sich wieder -> Auftrag wird abgearbeitet.
    w = FakeWorker(app, token)
    job = w.claim()
    assert job["eingabe"]["messages"][0]["content"].startswith("Bitte analysiere")
    assert w.finish(job, "Zusammenfassung vom lokalen Modell").status_code == 200
    final = admin.get(f"/zentriq/api/conversations/{conv}/messages/{reply_id}").get_json()["message"]
    assert final["status"] == "done" and final["content"] == "Zusammenfassung vom lokalen Modell"
    assert final["model"].startswith("Eigener Rechner") and final["local"] is None


def test_forced_local_choice_and_no_cloud_fallback_on_failure(app, world, fake):
    admin = login(app, "admin-a@example.com")
    options = admin.get("/zentriq/api/models").get_json()["models"]
    assert all(o["ref"] != "local" for o in options)  # ohne Zugang keine Auswahl
    resp = admin.post(f"/zentriq/api/conversations/{_conversation(admin)}/messages", json={"content": "Hi", "model": "local"})
    assert resp.status_code == 409 and resp.get_json()["code"] == "no_local_worker"
    token = _worker(app, admin)
    options = admin.get("/zentriq/api/models").get_json()["models"]
    assert any(o["ref"] == "local" and o["label"] == "Auf meinem Rechner bearbeiten" for o in options)
    conv = _conversation(admin, model="local")
    reply_id, reply = _send(admin, conv, "Kurze Frage")
    assert reply["status"] == "pending" and fake.calls == []
    w = FakeWorker(app, token)
    job = w.claim()
    w.post(f"/auftrag/{job['id']}/fehler", {"lease": job["lease"], "meldung": "Speicher voll", "wiederholen": False})
    final = admin.get(f"/zentriq/api/conversations/{conv}/messages/{reply_id}").get_json()["message"]
    assert final["status"] == "error" and "nicht an ein Cloud-Modell" in final["error"]
    assert fake.calls == []


def test_customer_data_allowed_when_processed_locally(app, world, fake):
    admin = login(app, "admin-a@example.com")
    _worker(app, admin)
    conv = _conversation(admin, model="local")
    _reply_id, reply = _send(admin, conv, "Max Mustermann, IBAN DE89 3704 0044 0532 0130 00 – fasse zusammen")
    assert reply["status"] == "pending"


# --- Kfz-Versicherungsschein ueber den Rechner --------------------------------------------------------


def _upload(client, **extra):
    data = {"schein": (io.BytesIO(_pdf(SCHEIN)), "schein.pdf"), **extra}
    resp = client.post("/kfz-angebot/versicherungsschein", data=data, content_type="multipart/form-data")
    assert resp.status_code == 302, resp.get_data(as_text=True)[:400]
    return int(resp.headers["Location"].split("/kfz-angebot/")[1].split("/")[0])


def _quote(job_id):
    db.session.expire_all()
    with bypass_tenant_scope():
        return db.session.get(HukQuoteJob, job_id)


def test_policy_goes_to_worker_and_result_is_merged(app, world, fake, monkeypatch):
    from app.services.ai import router

    monkeypatch.setattr(router, "complete", lambda **k: pytest.fail("kein Cloud-Modell fuer Dokumente"))
    admin = login(app, "admin-a@example.com")
    token = _worker(app, admin)
    assert "Auf meinem Rechner auslesen" in admin.get("/kfz-angebot/versicherungsschein").get_data(as_text=True)
    quote_id = _upload(admin)
    html = admin.get(f"/kfz-angebot/{quote_id}/angaben").get_data(as_text=True)
    assert "Dokument wird gelesen" in html and "nicht erreichbar" in html
    w = FakeWorker(app, token)
    job = w.claim(["kfz_policy_extract"])
    payload = job["eingabe"]
    assert payload["format"] == "json" and len(payload["images"]) == 1
    assert base64.b64decode(payload["images"][0])[:2] == b"\xff\xd8"  # JPEG-Seitenbild
    answer = {"versicherungsschein": {"hsn": "0603", "tsn": "BGU", "sf_haftpflicht": "SF 11", "werkstattbindung": "nein",
                                      "beitrag": "44,22", "zahlweise": "monatlich"}, "beitragsrechnung": {}}
    assert w.finish(job, json.dumps(answer)).status_code == 200
    quote = _quote(quote_id)
    assert quote.comparison["local"]["state"] == "done" and quote.comparison["local"]["found"] is None
    assert quote.input_data["hsn"] == "0603"
    # SF 12 (Zentriq) vs. SF 11 (Rechner): widerspruechlich -> leer und markiert.
    assert "sf_haftpflicht" not in quote.input_data and "SF 12 oder SF 11" in quote.comparison["flags"]["sf_haftpflicht"]
    html = admin.get(f"/kfz-angebot/{quote_id}/angaben").get_data(as_text=True)
    assert "Von Ihrem Rechner ausgelesen" in html and "Bei HUK berechnen" in html
    with bypass_tenant_scope():
        assert LocalAiJob.query.one().payload is None  # Seitenbilder geloescht


def test_policy_without_waiting_uses_zentriq_values(app, world, fake):
    admin = login(app, "admin-a@example.com")
    token = _worker(app, admin)
    quote_id = _upload(admin)
    admin.post(f"/kfz-angebot/{quote_id}/ohne-rechner")
    quote = _quote(quote_id)
    assert quote.comparison["local"]["state"] == "skipped" and quote.input_data["sf_haftpflicht"] == "SF 12"
    assert FakeWorker(app, token).claim() is None  # abgebrochen, wird nicht mehr vergeben
    assert "Ohne eigenen Rechner fortgefahren" in admin.get(f"/kfz-angebot/{quote_id}/angaben").get_data(as_text=True)


def test_reviewed_policy_values_feed_huk_calculation(app, world, fake, monkeypatch):
    from app.services.huk import runner
    from tests.test_kfz_vergleich import RESULT

    calls = []
    monkeypatch.setattr(runner, "run", lambda values, **k: calls.append(values) or dict(RESULT))
    admin = login(app, "admin-a@example.com")
    token = _worker(app, admin)
    quote_id = _upload(admin)
    w = FakeWorker(app, token)
    job = w.claim()
    w.finish(job, json.dumps({"versicherungsschein": {"hsn": "0603", "tsn": "BGU"}, "beitragsrechnung": {}}))
    quote = _quote(quote_id)
    values = {**quote.input_data, **MISSING_INPUT}
    data = {k: v for k, v in values.items() if k != "fahrer"}
    data.update(fahrer=values.get("fahrer", []), kunde="", aktion="berechnen", client_ref="x1",
                bisher_beitrag="44,22", bisher_zahlweise="monatlich", bisher_werkstatt="nein")
    assert admin.post(f"/kfz-angebot/{quote_id}/angaben", data=data).status_code == 302
    assert _quote(quote_id).status == "done" and calls and calls[0]["hsn"] == "0603"
    assert "70,12 € günstiger" in admin.get(f"/kfz-angebot/{quote_id}").get_data(as_text=True)


def test_policy_without_worker_keeps_previous_behaviour(app, world, fake):
    admin = login(app, "admin-a@example.com")
    quote_id = _upload(admin)
    assert "local" not in _quote(quote_id).comparison
    assert "Kfz-Vergleich: Angaben prüfen" in admin.get(f"/kfz-angebot/{quote_id}/angaben").get_data(as_text=True)


# --- Status, Verfall, Protokolle --------------------------------------------------------------------


def test_status_page_and_rights(app, world, fake):
    admin = login(app, "admin-a@example.com")
    token = _worker(app, admin)
    html = admin.get("/mein-rechner").get_data(as_text=True)
    assert "Nicht erreichbar" in html and "Laptop" in html
    FakeWorker(app, token).post("/hallo")
    data = admin.get("/mein-rechner/status").get_json()
    assert data["configured"] and data["online"] and data["model"] == "testmodell:1b"
    # Mitarbeiter: Status ja, Zugaenge nein.
    html = login(app, "dennis@example.com").get("/mein-rechner").get_data(as_text=True)
    assert "Erreichbar" in html and "Zugang anlegen" not in html and "Schlüssel" not in html


def test_old_queued_jobs_expire_and_inputs_are_deleted(app, world, fake):
    admin = login(app, "admin-a@example.com")
    token = _worker(app, admin)
    conv = _conversation(admin, model="local")
    reply_id, _reply = _send(admin, conv, "Kurze Frage")
    with bypass_tenant_scope():
        LocalAiJob.query.one().created_at = utcnow_naive() - timedelta(days=8)
        db.session.commit()
    assert FakeWorker(app, token).claim() is None
    with bypass_tenant_scope():
        job = LocalAiJob.query.one()
        assert job.status == "failed" and job.payload is None
        assert db.session.get(AiMessage, reply_id).status == "error"


def test_logs_contain_no_token_or_content(app, world, fake, caplog):
    caplog.set_level(logging.DEBUG)
    admin = login(app, "admin-a@example.com")
    token = _worker(app, admin)
    quote_id = _upload(admin)
    w = FakeWorker(app, token)
    job = w.claim()
    w.finish(job, json.dumps({"versicherungsschein": {"hsn": "0603"}}))
    FakeWorker(app, "zqw_falsch").post("/hallo")
    for secret in (token, "Beispielmann", "0603", "44,22", job["eingabe"]["images"][0][:40]):
        assert secret not in caplog.text
    assert quote_id
