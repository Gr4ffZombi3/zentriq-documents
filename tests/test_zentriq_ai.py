# ruff: noqa: F811  (Fixture "world" wird aus test_permissions_security importiert)
"""ZentriqAI: Zugriff (nur SUPER_ADMIN), Routing mit Fallback, Abkuehlzeiten, Kostengrenzen,
Kundendaten-Schutz, Gespraechsverwaltung, Kontext/Zusammenfassung, Cache, Einstellungen.

Anbieter werden nie echt aufgerufen: adapters.call / adapters.list_models sind ersetzt.
Zwei-Bueros-Szenario aus test_permissions_security (Buero A mit SUPER_ADMIN Justin)."""

from decimal import Decimal

import pytest

from app.models import AuditLog
from app.models.ai import AiConversation, AiMessage, AiModel, AiProviderSetting, AiUsage
from app.models.audit_log import AuditEventType
from app.services.ai import adapters, catalog, chat, router, state
from app.tenancy import bypass_tenant_scope
from tests.test_permissions_security import _user, login, world  # noqa: F401

GEMINI = "gemini:gemini-flash-latest"
GROQ = "groq:llama-3.3-70b-versatile"


class FakeProviders:
    """Ersetzt adapters.call: je Anbieter eine Liste von Ergebnissen/Fehlern (letztes bleibt)."""

    def __init__(self):
        self.behaviour: dict[str, list] = {}
        self.calls: list[dict] = []

    def set(self, provider, *items):
        self.behaviour[provider] = list(items)

    def __call__(self, provider, model_id, system, messages, max_tokens, timeout):
        self.calls.append({"provider": provider, "model": model_id, "system": system, "messages": messages})
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


def _send(client, conversation_id, content="Wie formuliere ich eine freundliche Erinnerung?", **extra):
    return client.post(f"/zentriq/api/conversations/{conversation_id}/messages", json={"content": content, **extra})


def _usage():
    with bypass_tenant_scope():
        return AiUsage.query.order_by(AiUsage.id).all()


# --- Zugriff --------------------------------------------------------------------------------


def test_only_super_admin_reaches_zentriq(app, world):
    assert _root(app, world).get("/zentriq").status_code == 200
    for email in ("admin-a@example.com", "dennis@example.com", "bob@example.com"):
        client = login(app, email)
        assert client.get("/zentriq").status_code == 403
        assert client.get("/zentriq/api/conversations", headers={"Accept": "application/json"}).status_code == 403
        assert client.get("/zentriq/einstellungen").status_code == 403


def test_disabled_switch(app, world):
    app.config["ZENTRIQ_AI_ENABLED"] = False
    client = _root(app, world)
    assert client.get("/zentriq").status_code == 503
    assert client.get("/zentriq/api/models").status_code == 503


def test_page_never_contains_api_keys(app, world, fake):
    client = _root(app, world)
    for url in ("/zentriq", "/zentriq/einstellungen", "/zentriq/api/models"):
        body = client.get(url).get_data(as_text=True)
        assert "GEHEIM" not in body


# --- Routing --------------------------------------------------------------------------------


def test_no_models_without_keys(app, world):
    client = _root(app, world)
    resp = _send(client, _new(client))
    assert resp.status_code == 503
    assert resp.get_json()["code"] == "no_models"


def test_free_first_and_fallback_on_rate_limit(app, world, fake):
    # Limits gelten bei Gemini je Modell: Flash 429 -> Flash-Lite 429 -> Groq antwortet.
    fake.set("gemini", adapters.ProviderError(adapters.RATE_LIMIT, retry_after=120, status=429))
    client = _root(app, world)
    conversation_id = _new(client)

    resp = _send(client, conversation_id)
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["message"]["provider"] == "groq"
    assert data["message"]["cost_usd"] == 0
    assert [c["model"] for c in fake.calls] == ["gemini-flash-latest", "gemini-flash-lite-latest", "llama-3.3-70b-versatile"]
    usage = _usage()
    assert [(u.provider, u.ok, u.error_type) for u in usage] == [
        ("gemini", False, "rate_limit"),
        ("gemini", False, "rate_limit"),
        ("groq", True, None),
    ]
    assert usage[2].input_tokens == 100 and usage[2].output_tokens == 50

    # Beide Gemini-Modelle sind jetzt pausiert (Retry-After 120 s) und werden uebersprungen.
    assert state.cooldown_info("gemini", "gemini-flash-latest")["seconds"] > 60
    fake.calls.clear()
    fake.set("gemini", adapters.ProviderResult("ok", 1, 1))
    resp = _send(client, conversation_id, "Noch eine Frage")
    assert [c["provider"] for c in fake.calls] == ["groq"]
    assert "Gemini Flash: pausiert" in resp.get_json()["attempts"]


def test_provider_outage_pauses_whole_provider(app, world, fake):
    fake.set("gemini", adapters.ProviderError(adapters.UNAVAILABLE, status=503))
    client = _root(app, world)
    resp = _send(client, _new(client))
    assert resp.get_json()["message"]["provider"] == "groq"
    # Nur ein Gemini-Versuch: danach ist der ganze Anbieter pausiert.
    assert [c["provider"] for c in fake.calls] == ["gemini", "groq"]


def test_all_failed_reports_attempts_and_keeps_user_message(app, world, fake):
    fake.set("gemini", adapters.ProviderError(adapters.UNAVAILABLE))
    fake.set("groq", adapters.ProviderError(adapters.AUTH))
    client = _root(app, world)
    conversation_id = _new(client)
    resp = _send(client, conversation_id)
    assert resp.status_code == 503
    data = resp.get_json()
    assert data["code"] == "all_failed"
    assert any("Groq" in a or "Llama" in a for a in data["attempts"])
    with bypass_tenant_scope():
        messages = AiMessage.query.filter_by(conversation_id=conversation_id).all()
    assert [m.role for m in messages] == ["user"]


def test_refusal_does_not_fall_back(app, world, fake):
    fake.set("gemini", adapters.ProviderError(adapters.REFUSAL))
    client = _root(app, world)
    resp = _send(client, _new(client))
    assert resp.status_code == 422
    assert [c["provider"] for c in fake.calls] == ["gemini"]


def test_manual_model_choice_and_fallback_flag(app, world, fake):
    client = _root(app, world)
    conversation_id = _new(client, model=GROQ)
    resp = _send(client, conversation_id)
    assert resp.get_json()["message"]["provider"] == "groq"
    assert fake.calls[0]["provider"] == "groq"

    fake.calls.clear()
    fake.set("groq", adapters.ProviderError(adapters.RATE_LIMIT))
    resp = _send(client, conversation_id, "Weiter")
    data = resp.get_json()
    assert data["fallback_used"] is True
    assert data["message"]["provider"] == "gemini"


def test_manual_fallback_can_be_disabled(app, world, fake):
    app.config["ZENTRIQ_AI_MANUAL_FALLBACK"] = False
    fake.set("groq", adapters.ProviderError(adapters.RATE_LIMIT))
    client = _root(app, world)
    resp = _send(client, _new(client, model=GROQ))
    assert resp.status_code == 503
    assert [c["provider"] for c in fake.calls] == ["groq"]


def test_unknown_model_choice_rejected(app, world, fake):
    client = _root(app, world)
    resp = _send(client, _new(client, model="anthropic:claude-opus-5-5"))
    assert resp.status_code == 400
    assert resp.get_json()["code"] == "model_unavailable"


def _enable_paid(app, db):
    app.config["ANTHROPIC_API_KEY"] = "sk-ant-GEHEIM"
    catalog.ensure_defaults()
    for model in AiModel.query.filter_by(provider="anthropic").all():
        model.enabled = model.model_id == "claude-haiku-4-5"
    for model in AiModel.query.filter(AiModel.provider != "anthropic").all():
        model.enabled = False
    db.session.commit()


def test_paid_models_blocked_by_budget(app, world, fake, db):
    _enable_paid(app, db)
    app.config["ZENTRIQ_AI_DAILY_BUDGET_USD"] = 0
    client = _root(app, world)
    resp = _send(client, _new(client))
    assert resp.status_code == 429
    assert resp.get_json()["code"] == "budget"
    assert fake.calls == []


def test_paid_model_cost_recorded(app, world, fake, db):
    _enable_paid(app, db)
    app.config["ZENTRIQ_AI_DAILY_BUDGET_USD"] = 1
    app.config["ZENTRIQ_AI_MONTHLY_BUDGET_USD"] = 10
    fake.set("anthropic", adapters.ProviderResult("Antwort", 1000, 500))
    client = _root(app, world)
    resp = _send(client, _new(client))
    assert resp.status_code == 200
    # Haiku 4.5: 1 USD / 5 USD je 1 Mio. Tokens -> 0,001 + 0,0025
    assert resp.get_json()["message"]["cost_usd"] == pytest.approx(0.0035)
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


def test_customer_data_blocked_outside_protected_mode(app, world, fake):
    client = _root(app, world)
    resp = _send(client, _new(client), "Kunde hat IBAN DE89 3704 0044 0532 0130 00")
    assert resp.status_code == 422
    assert resp.get_json()["code"] == "customer_data"
    assert fake.calls == []


def test_protected_mode_uses_only_approved_providers(app, world, fake, db):
    client = _root(app, world)
    conversation_id = _new(client, customer_data=True)
    resp = _send(client, conversation_id, "Kunde mit Vertragsnummer: 123456789 fragt nach Kündigung")
    assert resp.status_code == 503
    assert fake.calls == []

    AiProviderSetting.query.filter_by(provider="groq").one().customer_data_allowed = True
    db.session.commit()
    resp = _send(client, conversation_id, "Kunde mit Vertragsnummer: 123456789 fragt nach Kündigung")
    assert resp.status_code == 200
    assert {c["provider"] for c in fake.calls} == {"groq"}


# --- Gespraeche -----------------------------------------------------------------------------


def test_conversation_crud_and_search(app, world, fake):
    client = _root(app, world)
    conversation_id = _new(client)
    _send(client, conversation_id, "Wie plane ich die Urlaubsvertretung?")

    data = client.get(f"/zentriq/api/conversations/{conversation_id}").get_json()
    assert data["conversation"]["title"] == "Wie plane ich die Urlaubsvertretung?"
    assert [m["role"] for m in data["messages"]] == ["user", "assistant"]
    assert data["messages"][1]["model"] == "Gemini Flash"

    assert client.patch(f"/zentriq/api/conversations/{conversation_id}", json={"title": "Vertretung"}).status_code == 200
    assert client.patch(f"/zentriq/api/conversations/{conversation_id}", json={"title": "  "}).status_code == 400

    found = client.get("/zentriq/api/conversations?q=Urlaub").get_json()["conversations"]
    assert [c["id"] for c in found] == [conversation_id]
    assert client.get("/zentriq/api/conversations?q=nichtvorhanden").get_json()["conversations"] == []

    assert client.delete(f"/zentriq/api/conversations/{conversation_id}").status_code == 200
    assert client.get(f"/zentriq/api/conversations/{conversation_id}").status_code == 404
    with bypass_tenant_scope():
        assert AiMessage.query.count() == 0
        # Verbrauch bleibt fuer die Kostenrechnung, ohne Bezug zum Gespraech.
        assert all(u.conversation_id is None for u in AiUsage.query.all())
        assert AuditLog.query.filter_by(event_type=AuditEventType.AI_CONVERSATION_DELETED).count() == 1


def test_conversations_are_private_per_user(app, world, fake, db):
    from app.models import UserRole

    _user(db, world.tenant_b, "root-b@example.com", "08/0951-T", "Root B", UserRole.SUPER_ADMIN)
    client = _root(app, world)
    conversation_id = _new(client)
    _send(client, conversation_id)

    other = login(app, "root-b@example.com")
    assert other.get(f"/zentriq/api/conversations/{conversation_id}").status_code == 404
    assert _send(other, conversation_id).status_code == 404
    assert other.delete(f"/zentriq/api/conversations/{conversation_id}").status_code == 404
    assert other.get("/zentriq/api/conversations").get_json()["conversations"] == []


def test_delete_all(app, world, fake):
    client = _root(app, world)
    _new(client)
    _new(client)
    assert client.delete("/zentriq/api/conversations").get_json()["deleted"] == 2
    assert client.get("/zentriq/api/conversations").get_json()["conversations"] == []


# --- Kontext, Zusammenfassung, Cache ------------------------------------------------------


def test_context_is_limited_and_uses_summary(app, world, fake, db):
    app.config["ZENTRIQ_AI_CONTEXT_CHARS"] = 50
    client = _root(app, world)
    conversation_id = _new(client)
    for i in range(3):
        _send(client, conversation_id, f"Frage Nummer {i} " + "x" * 20)
    last = fake.calls[-1]
    # Nur die juengste Nachricht passt ins Budget; aeltere werden nicht erneut uebertragen.
    assert last["messages"] == [{"role": "user", "content": "Frage Nummer 2 " + "x" * 20}]

    with bypass_tenant_scope():
        conversation = db.session.get(AiConversation, conversation_id)
        conversation.summary = "Es geht um Frage 0 und 1."
        db.session.commit()
    _send(client, conversation_id, "Und jetzt?")
    assert "Es geht um Frage 0 und 1." in fake.calls[-1]["system"]


def test_summarize_older_messages(app, world, fake, db):
    client = _root(app, world)
    conversation_id = _new(client)
    for i in range(3):
        _send(client, conversation_id, f"Frage {i}")
    fake.set("gemini", adapters.ProviderResult("- Zusammenfassung", 10, 5))
    with bypass_tenant_scope():
        conversation = db.session.get(AiConversation, conversation_id)
        with app.test_request_context():
            from app.tenancy import use_tenant_id

            with use_tenant_id(conversation.tenant_id):
                assert chat.summarize(conversation, keep_recent=2) is True
        assert conversation.summary == "- Zusammenfassung"
        assert conversation.summary_upto_id is not None
    assert any(u.purpose == "summary" for u in _usage())


def test_summary_task_scheduled_after_threshold(app, world, fake, db):
    app.config["ZENTRIQ_AI_SUMMARY_AFTER_MESSAGES"] = 2
    client = _root(app, world)
    conversation_id = _new(client)
    _send(client, conversation_id, "Erste Frage")
    _send(client, conversation_id, "Zweite Frage")
    _send(client, conversation_id, "Dritte Frage")
    _send(client, conversation_id, "Vierte Frage")
    # Celery laeuft in Tests eager: Zusammenfassung ist bereits erstellt.
    assert any(u.purpose == "summary" for u in _usage())


def test_identical_request_served_from_cache(app, world, fake):
    app.config["ZENTRIQ_AI_CACHE_TTL_SECONDS"] = 600
    client = _root(app, world)
    first = _send(client, _new(client), "Was ist eine Leipziger Liste?").get_json()
    second = _send(client, _new(client), "Was ist eine Leipziger Liste?").get_json()
    assert len(fake.calls) == 1
    assert first["message"]["cached"] is False
    assert second["message"]["cached"] is True


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

    # Ausgeschalteter Anbieter wird nicht mehr verwendet.
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


def test_usage_endpoint(app, world, fake):
    client = _root(app, world)
    _send(client, _new(client))
    data = client.get("/zentriq/api/usage").get_json()
    assert data["own"]["rows"][0]["requests"] == 1
    assert data["budget"]["paid_allowed"] is False  # Standard: Budget 0


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
