# ruff: noqa: F811  (Fixture "world" wird aus test_permissions_security importiert)
"""ZentriqAI: Zugriff je Rolle, Routing mit Fallback, Abkuehlzeiten, Kostengrenzen,
Kundendaten-Schutz, asynchrone Antworten, Werkzeuge mit Rechtepruefung, Anhaenge,
Gespraechsverwaltung, Kontext/Zusammenfassung, Cache, Einstellungen, Startseite.

Anbieter werden nie echt aufgerufen: adapters.call / adapters.list_models sind ersetzt.
Celery laeuft in Tests eager - die Antwort ist nach dem POST bereits fertig.
Zwei-Bueros-Szenario aus test_permissions_security (Buero A: Admin A, Dennis, Laura,
SUPER_ADMIN Justin; Buero B: Admin B, Bob)."""

import io
import json
from decimal import Decimal

import pytest

from app.models import AuditLog
from app.models.ai import AiConversation, AiMessage, AiModel, AiProviderSetting, AiUsage
from app.models.audit_log import AuditEventType
from app.services.ai import adapters, catalog, chat, router, state
from app.tenancy import bypass_tenant_scope, use_tenant_id
from tests.test_permissions_security import login, world  # noqa: F401

GEMINI = "gemini:gemini-flash-latest"
GROQ = "groq:llama-3.3-70b-versatile"


class FakeProviders:
    """Ersetzt adapters.call: je Anbieter eine Liste von Ergebnissen/Fehlern (letztes bleibt)."""

    def __init__(self):
        self.behaviour: dict[str, list] = {}
        self.calls: list[dict] = []

    def set(self, provider, *items):
        self.behaviour[provider] = list(items)

    def __call__(self, provider, model_id, system, messages, max_tokens, timeout, tools=None):
        self.calls.append({"provider": provider, "model": model_id, "system": system, "messages": list(messages), "tools": tools})
        items = self.behaviour.get(provider) or [adapters.ProviderResult(f"Antwort von {provider}", 100, 50)]
        item = items.pop(0) if len(items) > 1 else items[0]
        if isinstance(item, Exception):
            raise item
        return item


@pytest.fixture(autouse=True)
def _reset_state():
    state.reset_memory()
    yield
    state.reset_memory()


@pytest.fixture()
def fake(app, monkeypatch):
    app.config["GEMINI_API_KEY"] = "gm-GEHEIM-123"
    app.config["GROQ_API_KEY"] = "gq-GEHEIM-456"
    app.config["ZENTRIQ_AI_CACHE_TTL_SECONDS"] = 0
    providers = FakeProviders()
    monkeypatch.setattr(adapters, "call", providers)
    return providers


def _root(app, world):
    return login(app, "justin@example.com")


def _new(client, **body):
    resp = client.post("/zentriq/api/conversations", json=body)
    assert resp.status_code == 201
    return resp.get_json()["conversation"]["id"]


class Sent:
    """POST .../messages (202) plus die fertige Antwort, wie sie der Browser abfragt."""

    def __init__(self, client, conversation_id, resp):
        self.status_code = resp.status_code
        self.data = resp.get_json()
        self.reply = None
        if resp.status_code == 202:
            reply_id = self.data["reply"]["id"]
            self.reply = client.get(f"/zentriq/api/conversations/{conversation_id}/messages/{reply_id}").get_json()["message"]


def _send(client, conversation_id, content="Wie formuliere ich eine freundliche Erinnerung?", **extra):
    resp = client.post(f"/zentriq/api/conversations/{conversation_id}/messages", json={"content": content, **extra})
    return Sent(client, conversation_id, resp)


def _usage():
    with bypass_tenant_scope():
        return AiUsage.query.order_by(AiUsage.id).all()


def _approve(db, provider="groq"):
    catalog.ensure_defaults()
    AiProviderSetting.query.filter_by(provider=provider).one().customer_data_allowed = True
    db.session.commit()


# --- Zugriff und Startseite -----------------------------------------------------------------


@pytest.mark.parametrize("email", ["justin@example.com", "admin-a@example.com", "dennis@example.com", "bob@example.com"])
def test_every_role_reaches_chat_but_only_super_admin_settings(app, world, email):
    client = login(app, email)
    assert client.get("/zentriq").status_code == 200
    assert client.get("/zentriq/api/conversations").status_code == 200
    is_root = email == "justin@example.com"
    assert client.get("/zentriq/einstellungen").status_code == (200 if is_root else 403)
    assert client.post("/zentriq/einstellungen/anbieter/groq", data={"enabled": "1"}).status_code == (302 if is_root else 403)


@pytest.mark.parametrize("email", ["justin@example.com", "admin-a@example.com", "dennis@example.com"])
def test_home_opens_chat(app, world, email):
    resp = login(app, email).get("/")
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("/zentriq")


def test_home_falls_back_when_disabled(app, world):
    app.config["ZENTRIQ_AI_ENABLED"] = False
    assert login(app, "dennis@example.com").get("/").headers["Location"].endswith("/uebersicht")
    client = _root(app, world)
    assert client.get("/zentriq").status_code == 503
    assert client.get("/zentriq/api/models").status_code == 503


def test_page_never_contains_api_keys(app, world, fake):
    client = _root(app, world)
    for url in ("/zentriq", "/zentriq/einstellungen", "/zentriq/api/models"):
        assert "GEHEIM" not in client.get(url).get_data(as_text=True)


def test_layout_has_sidebar_areas_and_time_clock(app, world):
    body = login(app, "dennis@example.com").get("/zentriq").get_data(as_text=True)
    assert "Neuer Chat" in body and "Chats durchsuchen" in body
    assert "Leipziger Liste" in body and "Zeiterfassung" in body
    assert "data-zs-clock" in body
    # SUPER_ADMIN: keine Zeiterfassung, keine Buerobereiche, aber KI-Einstellungen.
    body = _root(app, world).get("/zentriq").get_data(as_text=True)
    assert "data-zs-clock" not in body
    assert "Leipziger Liste" not in body
    assert "KI-Einstellungen" in body


def test_existing_pages_keep_working_with_new_layout(app, world):
    client = login(app, "admin-a@example.com")
    for url in ("/uebersicht", "/zeiterfassung", "/leipziger-liste", "/settings/profile"):
        resp = client.get(url, follow_redirects=True)
        assert resp.status_code == 200, url
        assert "data-zs-sidebar" in resp.get_data(as_text=True)


# --- Routing --------------------------------------------------------------------------------


def test_no_models_without_keys(app, world):
    client = _root(app, world)
    sent = _send(client, _new(client))
    assert sent.status_code == 202
    assert sent.reply["status"] == "error"
    assert "kein KI-Modell verfügbar" in sent.reply["error"]


def test_free_first_and_fallback_on_rate_limit(app, world, fake):
    # Komplexe Anfrage ("formuliere") -> starke kostenlose Modelle zuerst: Gemini Flash 429 -> Llama 70B.
    fake.set("gemini", adapters.ProviderError(adapters.RATE_LIMIT, retry_after=120, status=429))
    client = _root(app, world)
    conversation_id = _new(client)

    reply = _send(client, conversation_id).reply
    assert reply["status"] == "done"
    assert reply["provider"] == "groq"
    assert reply["cost_usd"] == 0
    assert [c["model"] for c in fake.calls] == ["gemini-flash-latest", "llama-3.3-70b-versatile"]
    usage = _usage()
    assert [(u.provider, u.ok, u.error_type) for u in usage] == [("gemini", False, "rate_limit"), ("groq", True, None)]
    assert usage[1].input_tokens == 100 and usage[1].output_tokens == 50

    # Gemini Flash ist pausiert (Retry-After 120 s) und wird uebersprungen.
    assert state.cooldown_info("gemini", "gemini-flash-latest")["seconds"] > 60
    fake.calls.clear()
    fake.set("gemini", adapters.ProviderResult("ok", 1, 1))
    _send(client, conversation_id, "Formuliere noch eine Variante")
    assert [c["model"] for c in fake.calls] == ["llama-3.3-70b-versatile"]


def test_simple_requests_use_small_models_first(app, world, fake):
    client = _root(app, world)
    _send(client, _new(client), "Hallo!")
    assert fake.calls[-1]["model"] == "gemini-flash-lite-latest"
    _send(client, _new(client), "Analysiere bitte diesen Ablauf und bewerte ihn.")
    assert fake.calls[-1]["model"] == "gemini-flash-latest"


def test_provider_outage_pauses_whole_provider(app, world, fake):
    fake.set("gemini", adapters.ProviderError(adapters.UNAVAILABLE, status=503))
    client = _root(app, world)
    assert _send(client, _new(client)).reply["provider"] == "groq"
    assert [c["provider"] for c in fake.calls] == ["gemini", "groq"]


def test_all_failed_reports_attempts_and_keeps_user_message(app, world, fake):
    fake.set("gemini", adapters.ProviderError(adapters.UNAVAILABLE))
    fake.set("groq", adapters.ProviderError(adapters.AUTH))
    client = _root(app, world)
    conversation_id = _new(client)
    reply = _send(client, conversation_id).reply
    assert reply["status"] == "error"
    assert "Zugang ungültig" in reply["error"]
    with bypass_tenant_scope():
        messages = AiMessage.query.filter_by(conversation_id=conversation_id).order_by(AiMessage.id).all()
    assert [(m.role, m.status) for m in messages] == [("user", "done"), ("assistant", "error")]
    # Erneut senden: die Fehlantwort geht nicht in den Kontext, die Benutzernachrichten schon.
    fake.set("groq", adapters.ProviderResult("jetzt ok", 1, 1))
    state.reset_memory()
    assert _send(client, conversation_id, "Nochmal").reply["status"] == "done"
    assert [m["role"] for m in fake.calls[-1]["messages"]] == ["user"]


def test_refusal_does_not_fall_back(app, world, fake):
    fake.set("gemini", adapters.ProviderError(adapters.REFUSAL))
    client = _root(app, world)
    assert _send(client, _new(client)).reply["status"] == "error"
    assert [c["provider"] for c in fake.calls] == ["gemini"]


def test_manual_model_choice_and_fallback_note(app, world, fake):
    client = _root(app, world)
    conversation_id = _new(client, model=GROQ)
    assert _send(client, conversation_id).reply["provider"] == "groq"
    assert fake.calls[0]["provider"] == "groq"

    fake.calls.clear()
    fake.set("groq", adapters.ProviderError(adapters.RATE_LIMIT))
    reply = _send(client, conversation_id, "Weiter").reply
    assert reply["provider"] == "gemini"
    assert "nicht verfügbar" in reply["error"]


def test_manual_fallback_can_be_disabled(app, world, fake):
    app.config["ZENTRIQ_AI_MANUAL_FALLBACK"] = False
    fake.set("groq", adapters.ProviderError(adapters.RATE_LIMIT))
    client = _root(app, world)
    assert _send(client, _new(client, model=GROQ)).reply["status"] == "error"
    assert [c["provider"] for c in fake.calls] == ["groq"]


def test_unknown_model_choice_rejected(app, world, fake):
    client = _root(app, world)
    reply = _send(client, _new(client, model="anthropic:claude-opus-5-5")).reply
    assert reply["status"] == "error"
    assert "nicht verfügbar" in reply["error"]


def _enable_paid(app, db):
    app.config["ANTHROPIC_API_KEY"] = "sk-ant-GEHEIM"
    catalog.ensure_defaults()
    for model in AiModel.query.all():
        model.enabled = model.model_id == "claude-haiku-4-5"
    db.session.commit()


def test_paid_models_blocked_by_budget(app, world, fake, db):
    _enable_paid(app, db)
    app.config["ZENTRIQ_AI_DAILY_BUDGET_USD"] = 0
    client = _root(app, world)
    reply = _send(client, _new(client)).reply
    assert reply["status"] == "error" and "Kostengrenze" in reply["error"]
    assert fake.calls == []


def test_paid_model_cost_recorded(app, world, fake, db):
    _enable_paid(app, db)
    app.config["ZENTRIQ_AI_DAILY_BUDGET_USD"] = 1
    app.config["ZENTRIQ_AI_MONTHLY_BUDGET_USD"] = 10
    fake.set("anthropic", adapters.ProviderResult("Antwort", 1000, 500))
    client = _root(app, world)
    reply = _send(client, _new(client)).reply
    # Haiku 4.5: 1 USD / 5 USD je 1 Mio. Tokens -> 0,001 + 0,0025
    assert reply["cost_usd"] == pytest.approx(0.0035)
    assert router.budget_status()["today"] == Decimal("0.0035")


def test_paid_model_without_price_never_selected(app, world, fake, db):
    app.config["OPENROUTER_API_KEY"] = "or-key"
    db.session.add(AiModel(provider="openrouter", model_id="vendor/paid-model", label="Paid", enabled=True, priority=1, is_free=False))
    db.session.commit()
    assert all(m.model_id != "vendor/paid-model" for m in router.available_models())


# --- Kundendaten ----------------------------------------------------------------------------


def test_customer_data_detection():
    assert "IBAN" in chat.detect_customer_data("IBAN DE89 3704 0044 0532 0130 00")
    assert "E-Mail-Adresse" in chat.detect_customer_data("bitte an max@beispiel.de")
    assert "Telefonnummer" in chat.detect_customer_data("Rückruf unter 0171 2345678")
    assert "Kunden-/Vertragsnummer" in chat.detect_customer_data("Vertragsnummer: 123456789")
    assert chat.detect_customer_data("Schreibe eine freundliche Erinnerung an einen Termin.") == []


def test_customer_data_pseudonymized_outside_protected_mode(app, world, fake):
    fake.set("gemini", adapters.ProviderResult("Die IBAN [IBAN 1] ist notiert.", 10, 5))
    client = _root(app, world)
    reply = _send(client, _new(client), "Kunde hat IBAN DE89 3704 0044 0532 0130 00, bitte merken").reply
    sent = fake.calls[-1]["messages"][-1]["content"]
    assert "DE89" not in sent and "[IBAN 1]" in sent
    # Lokal wieder eingesetzt: der Benutzer sieht den echten Wert.
    assert reply["content"] == "Die IBAN DE89 3704 0044 0532 0130 00 ist notiert."


def test_protected_mode_uses_only_approved_providers(app, world, fake, db):
    client = _root(app, world)
    conversation_id = _new(client, customer_data=True)
    reply = _send(client, conversation_id, "Kunde mit Vertragsnummer: 123456789 fragt nach Kündigung").reply
    assert reply["status"] == "error"
    assert fake.calls == []

    _approve(db, "groq")
    reply = _send(client, conversation_id, "Kunde mit Vertragsnummer: 123456789 fragt nach Kündigung").reply
    assert reply["status"] == "done"
    assert {c["provider"] for c in fake.calls} == {"groq"}


# --- Werkzeuge ------------------------------------------------------------------------------


def test_tools_offered_per_role_pseudonymized_without_approval(app, world, fake, db):
    question = "Hilf mir, meinen Tag zu planen."
    client = login(app, "dennis@example.com")
    _send(client, _new(client), question)
    # Ohne freigegebenen Anbieter: Werkzeuge ja, aber pseudonymisiert (Hinweis im Systemtext).
    employee_tools = {t["name"] for t in fake.calls[-1]["tools"]}
    assert employee_tools == {"tagesueberblick", "meine_arbeitszeiten", "offene_vorgaenge", "leipziger_auswertung", "suche"}
    assert "Platzhalter" in fake.calls[-1]["system"]

    # Mit freigegebenem Anbieter: Buerodaten nur dorthin, ohne Pseudonymisierung.
    _approve(db, "groq")
    _send(client, _new(client), question)
    assert fake.calls[-1]["provider"] == "groq"
    assert "Platzhalter" not in fake.calls[-1]["system"]

    admin = login(app, "admin-a@example.com")
    _send(admin, _new(admin), question)
    assert {t["name"] for t in fake.calls[-1]["tools"]} == employee_tools | {
        "aufgaben", "team_anwesenheit", "mitarbeiter_aktivitaet", "sprachnachrichten", "aufgabe_erledigen"
    }

    # SUPER_ADMIN: keine Buerodaten, keine Werkzeuge.
    root = _root(app, world)
    _send(root, _new(root), question)
    assert fake.calls[-1]["tools"] is None


def test_tool_call_executes_existing_service(app, world, fake, db):
    _approve(db, "groq")
    fake.set(
        "groq",
        adapters.ProviderResult("", 10, 5, tool_calls=[adapters.ToolCall("c1", "meine_arbeitszeiten", {"zeitraum": "woche"})]),
        adapters.ProviderResult("Sie haben diese Woche **8:00** Stunden gearbeitet.", 20, 10),
    )
    client = login(app, "dennis@example.com")
    reply = _send(client, _new(client), "Wie viele Stunden fehlen mir diese Woche bis zum Soll? Erkläre kurz.").reply
    assert reply["status"] == "done"
    assert reply["tools"] == ["Arbeitszeiten"]
    assert reply["input_tokens"] == 30 and reply["output_tokens"] == 15
    second = fake.calls[-1]["messages"]
    assert second[-2]["role"] == "assistant" and second[-2]["tool_calls"][0]["name"] == "meine_arbeitszeiten"
    result = json.loads(second[-1]["content"])
    assert "netto_gesamt" in result and "status_jetzt" in result


def test_tool_permissions_rechecked_on_execution(app, world, db):
    from app.models import User
    from app.services.ai import tools

    with bypass_tenant_scope():
        dennis = db.session.get(User, world.dennis_id)
    with use_tenant_id(world.tenant_a.id):
        assert "fehler" in json.loads(tools.execute(dennis, "aufgaben", {"status": "offen"}))
        assert "fehler" in json.loads(tools.execute(dennis, "gibt_es_nicht", {}))


def test_employee_search_tool_never_returns_customer_master_data(app, world, db):
    from app.models import User
    from app.services.ai import tools

    with bypass_tenant_scope():
        dennis = db.session.get(User, world.dennis_id)
    with use_tenant_id(world.tenant_a.id):
        result = json.loads(tools.execute(dennis, "suche", {"suchbegriff": "OFFEN"}))
    assert result["kunden"] == []
    assert all("LAURA" not in json.dumps(v) for v in result["vorgaenge"])


# --- Anhaenge -------------------------------------------------------------------------------


def _upload(client, conversation_id, filename, content, text="Bitte zusammenfassen", mimetype="text/plain"):
    resp = client.post(
        f"/zentriq/api/conversations/{conversation_id}/messages",
        data={"content": text, "files": (io.BytesIO(content), filename, mimetype)},
        content_type="multipart/form-data",
    )
    return Sent(client, conversation_id, resp)


def test_text_attachment_is_sent_as_context(app, world, fake):
    client = _root(app, world)
    sent = _upload(client, _new(client), "notiz.txt", "Termin am Montag mit dem Team.".encode())
    assert sent.reply["status"] == "done"
    assert sent.data["message"]["attachments"] == [{"filename": "notiz.txt", "kind": "document"}]
    assert "[Anhang: notiz.txt]" in fake.calls[-1]["messages"][-1]["content"]


def test_pdf_attachment_extracted(app, world, fake):
    import fitz

    pdf = fitz.open()
    pdf.new_page().insert_text((72, 72), "Leipziger Liste Seite eins")
    client = _root(app, world)
    _upload(client, _new(client), "liste.pdf", pdf.tobytes(), mimetype="application/pdf")
    assert "Leipziger Liste Seite eins" in fake.calls[-1]["messages"][-1]["content"]


def test_attachment_with_customer_data_requires_protected_mode(app, world, fake):
    client = _root(app, world)
    sent = _upload(client, _new(client), "kunde.txt", "Kundennummer: 4711000 Rückruf 0171 2345678".encode())
    assert sent.status_code == 422
    assert fake.calls == []


def test_invalid_files_rejected(app, world, fake):
    client = _root(app, world)
    conversation_id = _new(client)
    assert _upload(client, conversation_id, "virus.exe", b"MZ...").status_code == 400
    assert _upload(client, conversation_id, "fake.pdf", b"not a pdf", mimetype="application/pdf").status_code == 400


def test_audio_requires_protected_mode(app, world, fake, monkeypatch):
    from app.services import memo

    monkeypatch.setattr(memo, "validate_audio", lambda filename, content, mimetype=None: filename)
    client = _root(app, world)
    assert _upload(client, _new(client), "anruf.mp3", b"ID3fake", mimetype="audio/mpeg").status_code == 422


def test_audio_transcribed_in_worker_and_temp_file_removed(app, world, fake, db, monkeypatch, tmp_path):
    from app.services import memo

    app.config["TRANSCRIPTION_TMP_DIR"] = str(tmp_path / "memo")
    monkeypatch.setattr(memo, "validate_audio", lambda filename, content, mimetype=None: filename)
    monkeypatch.setattr(memo, "transcribe_file", lambda path: "Hallo, hier ist Herr Muster, bitte Rückruf.")
    _approve(db, "groq")
    client = _root(app, world)
    sent = _upload(client, _new(client, customer_data=True), "anruf.mp3", b"ID3fake", text="", mimetype="audio/mpeg")
    assert sent.reply["status"] == "done"
    content = fake.calls[-1]["messages"][-1]["content"]
    assert "Transkript der Sprachnachricht: anruf.mp3" in content and "Herr Muster" in content
    assert list((tmp_path / "memo").glob("*.audio")) == []


# --- Gespraeche -----------------------------------------------------------------------------


def test_conversation_crud_search_pin_project(app, world, fake):
    client = _root(app, world)
    conversation_id = _new(client)
    _send(client, conversation_id, "Wie plane ich die Urlaubsvertretung?")

    data = client.get(f"/zentriq/api/conversations/{conversation_id}").get_json()
    assert data["conversation"]["title"] == "Wie plane ich die Urlaubsvertretung?"
    assert [m["role"] for m in data["messages"]] == ["user", "assistant"]
    assert data["messages"][1]["model"] == "Gemini Flash"

    assert client.patch(f"/zentriq/api/conversations/{conversation_id}", json={"title": "Vertretung"}).status_code == 200
    assert client.patch(f"/zentriq/api/conversations/{conversation_id}", json={"title": "  "}).status_code == 400
    client.patch(f"/zentriq/api/conversations/{conversation_id}", json={"pinned": True, "project": "Urlaub 2027"})
    pinned = client.get("/zentriq/api/conversations?pinned=1").get_json()["conversations"]
    assert [c["id"] for c in pinned] == [conversation_id] and pinned[0]["project"] == "Urlaub 2027"
    assert client.get("/zentriq/api/conversations?pinned=0").get_json()["conversations"] == []
    assert client.get("/zentriq/api/projects").get_json()["projects"] == ["Urlaub 2027"]

    found = client.get("/zentriq/api/conversations?q=Urlaubsvertretung").get_json()["conversations"]
    assert [c["id"] for c in found] == [conversation_id]
    assert client.get("/zentriq/api/conversations?q=nichtvorhanden").get_json()["conversations"] == []
    assert client.get(f"/zentriq/c/{conversation_id}").status_code == 200

    assert client.delete(f"/zentriq/api/conversations/{conversation_id}").status_code == 200
    assert client.get(f"/zentriq/api/conversations/{conversation_id}").status_code == 404
    with bypass_tenant_scope():
        assert AiMessage.query.count() == 0
        assert all(u.conversation_id is None for u in AiUsage.query.all())
        assert AuditLog.query.filter_by(event_type=AuditEventType.AI_CONVERSATION_DELETED).count() == 1


def test_conversations_are_private_per_user_and_office(app, world, fake):
    dennis = login(app, "dennis@example.com")
    conversation_id = _new(dennis)
    _send(dennis, conversation_id)

    for email in ("laura@example.com", "admin-a@example.com", "bob@example.com", "justin@example.com"):
        other = login(app, email)
        assert other.get(f"/zentriq/api/conversations/{conversation_id}").status_code == 404
        assert other.post(f"/zentriq/api/conversations/{conversation_id}/messages", json={"content": "x"}).status_code == 404
        assert other.delete(f"/zentriq/api/conversations/{conversation_id}").status_code == 404
        assert other.get("/zentriq/api/conversations").get_json()["conversations"] == []


def test_delete_all(app, world, fake):
    client = _root(app, world)
    _new(client)
    _new(client)
    assert client.delete("/zentriq/api/conversations").get_json()["deleted"] == 2
    assert client.get("/zentriq/api/conversations").get_json()["conversations"] == []


def test_busy_while_pending_and_stale_pending_expires(app, world, fake, db):
    from datetime import timedelta

    from app.models.ai import _now

    client = _root(app, world)
    conversation_id = _new(client)
    with bypass_tenant_scope():
        conversation = db.session.get(AiConversation, conversation_id)
        pending = AiMessage(tenant_id=conversation.tenant_id, conversation_id=conversation_id, role="assistant", content="", status="pending")
        db.session.add(pending)
        db.session.commit()
        pending_id = pending.id
    assert _send(client, conversation_id).status_code == 409
    with bypass_tenant_scope():
        db.session.get(AiMessage, pending_id).created_at = _now() - timedelta(minutes=10)
        db.session.commit()
    reply = client.get(f"/zentriq/api/conversations/{conversation_id}/messages/{pending_id}").get_json()["message"]
    assert reply["status"] == "error"
    assert _send(client, conversation_id).status_code == 202


# --- Kontext, Zusammenfassung, Cache ------------------------------------------------------


def test_context_is_limited_and_uses_summary_and_project(app, world, fake, db):
    app.config["ZENTRIQ_AI_CONTEXT_CHARS"] = 50
    client = _root(app, world)
    conversation_id = _new(client, project="Kfz")
    for i in range(3):
        _send(client, conversation_id, f"Frage Nummer {i} " + "x" * 20)
    assert fake.calls[-1]["messages"] == [{"role": "user", "content": "Frage Nummer 2 " + "x" * 20}]

    other_id = _new(client, project="Kfz")
    with bypass_tenant_scope():
        db.session.get(AiConversation, conversation_id).summary = "Es geht um Fahrzeugwechsel."
        db.session.commit()
    _send(client, other_id, "Und jetzt?")
    assert "Es geht um Fahrzeugwechsel." in fake.calls[-1]["system"]


def test_summarize_older_messages(app, world, fake, db):
    client = _root(app, world)
    conversation_id = _new(client)
    for i in range(3):
        _send(client, conversation_id, f"Frage {i}")
    fake.set("gemini", adapters.ProviderResult("- Zusammenfassung", 10, 5))
    with bypass_tenant_scope():
        conversation = db.session.get(AiConversation, conversation_id)
    with use_tenant_id(conversation.tenant_id):
        assert chat.summarize(conversation, keep_recent=2) is True
    assert conversation.summary == "- Zusammenfassung"
    assert any(u.purpose == "summary" for u in _usage())


def test_summary_task_scheduled_after_threshold(app, world, fake):
    app.config["ZENTRIQ_AI_SUMMARY_AFTER_MESSAGES"] = 2
    client = _root(app, world)
    conversation_id = _new(client)
    for text in ("Erste Frage", "Zweite Frage", "Dritte Frage", "Vierte Frage"):
        _send(client, conversation_id, text)
    assert any(u.purpose == "summary" for u in _usage())


def test_identical_request_served_from_cache(app, world, fake):
    app.config["ZENTRIQ_AI_CACHE_TTL_SECONDS"] = 600
    client = _root(app, world)
    first = _send(client, _new(client), "Was ist eine Leipziger Liste?").reply
    second = _send(client, _new(client), "Was ist eine Leipziger Liste?").reply
    assert len(fake.calls) == 1
    assert first["cached"] is False and second["cached"] is True


# --- Zeiterfassungsmodul --------------------------------------------------------------------


def test_time_clock_json_uses_existing_timetracking(app, world):
    client = login(app, "laura@example.com")
    headers = {"Accept": "application/json"}
    if client.get("/zeiterfassung/status").get_json()["status"] != "out":
        client.post("/zeiterfassung/ausstempeln", headers=headers)
    resp = client.post("/zeiterfassung/einstempeln", headers=headers)
    assert resp.status_code == 200 and resp.get_json()["status"] == "in"
    # Doppelt einstempeln: bestehende Regel greift, Antwort mit Fehler und aktuellem Stand.
    resp = client.post("/zeiterfassung/einstempeln", headers=headers)
    assert resp.status_code == 409 and resp.get_json()["status"] == "in"
    assert client.post("/zeiterfassung/pause/start", headers=headers).get_json()["status"] == "break"
    assert client.post("/zeiterfassung/pause/ende", headers=headers).get_json()["status"] == "in"
    assert client.post("/zeiterfassung/ausstempeln", headers=headers).get_json()["status"] == "out"
    with bypass_tenant_scope():
        events = {e.event_type for e in AuditLog.query.filter(AuditLog.actor_email_snapshot == "laura@example.com").all()}
    assert {AuditEventType.TIME_CLOCK_IN, AuditEventType.TIME_CLOCK_OUT} <= events
    # Formular-Stempeln (ohne JSON) funktioniert wie bisher mit Weiterleitung.
    assert client.post("/zeiterfassung/einstempeln").status_code == 302


def test_super_admin_has_no_time_clock_endpoint(app, world):
    assert _root(app, world).get("/zeiterfassung/status").status_code == 403


# --- Einstellungen und Verbrauch ------------------------------------------------------------


def test_settings_change_model_and_provider(app, world, fake, db):
    client = _root(app, world)
    assert client.get("/zentriq/einstellungen").status_code == 200
    model = AiModel.query.filter_by(provider="gemini", model_id="gemini-flash-latest").one()
    client.post(
        f"/zentriq/einstellungen/modelle/{model.id}",
        data={"label": "Gemini Schnell", "priority": "5", "is_free": "1", "input_price": "", "output_price": ""},
    )
    db.session.expire_all()
    assert model.enabled is False and model.label == "Gemini Schnell" and model.priority == 5

    client.post("/zentriq/einstellungen/anbieter/groq", data={"enabled": "1", "customer_data_allowed": "1"})
    db.session.expire_all()
    assert AiProviderSetting.query.filter_by(provider="groq").one().customer_data_allowed is True
    with bypass_tenant_scope():
        assert AuditLog.query.filter_by(event_type=AuditEventType.AI_SETTINGS_CHANGED).count() == 2

    client.post("/zentriq/einstellungen/anbieter/groq", data={})
    refs = [m["ref"] for m in client.get("/zentriq/api/models").get_json()["models"]]
    assert GROQ not in refs and GEMINI not in refs


def test_discover_models(app, world, fake, monkeypatch, db):
    monkeypatch.setattr(adapters, "list_models", lambda key: ["llama-3.3-70b-versatile", "neues-modell"])
    client = _root(app, world)
    client.get("/zentriq/einstellungen")
    client.post("/zentriq/einstellungen/anbieter/groq/abrufen")
    new = AiModel.query.filter_by(provider="groq", model_id="neues-modell").one()
    assert new.enabled is False and new.is_free is True


def test_usage_endpoint_budget_only_for_super_admin(app, world, fake):
    client = login(app, "dennis@example.com")
    _send(client, _new(client))
    data = client.get("/zentriq/api/usage").get_json()
    assert data["own"]["rows"][0]["requests"] == 1
    assert "budget" not in data
    assert _root(app, world).get("/zentriq/api/usage").get_json()["budget"]["paid_allowed"] is False


def test_purge_command_removes_only_old_conversations(app, world, fake, db):
    from datetime import timedelta

    from app.models.ai import _now

    client = _root(app, world)
    old_id = _new(client)
    new_id = _new(client)
    with bypass_tenant_scope():
        db.session.get(AiConversation, old_id).updated_at = _now() - timedelta(days=200)
        db.session.commit()
    result = app.test_cli_runner().invoke(args=["zentriq-ai-purge"])
    assert "1 Gespraeche geloescht" in result.output
    with bypass_tenant_scope():
        assert {c.id for c in AiConversation.query.all()} == {new_id}
