# ruff: noqa: F811  (Fixture "world" wird aus test_permissions_security importiert)
"""KI-Assistent: Rollen, Datensparsamkeit, API-Fehler, kein API-Key im Client, Plattform-Panel.

Der Anthropic-Client wird durch eine Attrappe ersetzt - es gibt nie einen echten API-Aufruf.
Zwei-Bueros-Szenario aus test_permissions_security (Buero A: Admin A, Dennis, Laura,
SUPER_ADMIN Justin; Buero B: Admin B, Bob)."""

import logging
from types import SimpleNamespace

import anthropic
import httpx2
import pytest

from app.models import AuditLog, Customer
from app.models.audit_log import AuditEventType
from app.services import assistant
from app.tenancy import bypass_tenant_scope, use_tenant_id
from tests.test_permissions_security import login, world  # noqa: F401

SECRET_KEY = "sk-ant-test-GEHEIM-1234567890"
USER_TEXT = "Kunde möchte Fahrleistung von 12000 auf 17000 erhöhen. Änderung erledigt."


class FakeMessages:
    def __init__(self, behaviour):
        self.behaviour = behaviour
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if isinstance(self.behaviour, Exception):
            raise self.behaviour
        return self.behaviour


class FakeClient:
    def __init__(self, behaviour):
        self.beta = SimpleNamespace(messages=FakeMessages(behaviour))
        self.models = SimpleNamespace(retrieve=lambda model: SimpleNamespace(id=model))


def _response(text="Betreff: Änderung Ihrer Fahrleistung\n\nSehr geehrte Damen und Herren, ...", stop_reason="end_turn"):
    return SimpleNamespace(content=[SimpleNamespace(type="text", text=text)], stop_reason=stop_reason)


@pytest.fixture()
def enabled(app, monkeypatch):
    app.config["ANTHROPIC_API_KEY"] = SECRET_KEY
    app.config["ASSISTANT_ENABLED"] = True
    client = FakeClient(_response())
    monkeypatch.setattr(assistant, "get_client", lambda: client)
    return client


def _ask(client, action="email", text=USER_TEXT):
    return client.post("/assistent/anfrage", json={"action": action, "text": text})


def _audit():
    with bypass_tenant_scope():
        return AuditLog.query.filter_by(event_type=AuditEventType.ASSISTANT_USED).all()


# --- Rollen ---------------------------------------------------------------------------------


def test_office_admin_and_employee_can_use_assistant(app, world, enabled):
    for email in ("admin-a@example.com", "dennis@example.com", "bob@example.com"):
        resp = _ask(login(app, email))
        assert resp.status_code == 200, email
        assert resp.get_json()["result"].startswith("Betreff:")


def test_super_admin_cannot_use_assistant(app, world, enabled):
    client = login(app, "justin@example.com")
    assert _ask(client).status_code == 403
    assert enabled.beta.messages.calls == []
    resp = client.get("/plattform")
    assert resp.status_code == 200
    assert "data-assistant-toggle" not in resp.get_data(as_text=True)


def test_assistant_requires_login(app, world, enabled):
    resp = _ask(app.test_client())
    assert resp.status_code in (302, 401)
    assert enabled.beta.messages.calls == []


# --- Datensparsamkeit -----------------------------------------------------------------------


def test_only_user_text_and_fixed_instruction_are_sent(app, world, db, enabled):
    with use_tenant_id(world.tenant_a.id):
        db.session.add(Customer(tenant_id=world.tenant_a.id, name="Geheim Kundin", phone="0171 9999999", customer_number="KD-777"))
        db.session.commit()
    assert _ask(login(app, "dennis@example.com"), action="gespraechsnotiz").status_code == 200

    call = enabled.beta.messages.calls[0]
    assert call["messages"] == [{"role": "user", "content": USER_TEXT}]
    assert call["system"] == f"{assistant.BASE_INSTRUCTION}\n\nAufgabe: {assistant.ACTIONS['gespraechsnotiz'].instruction}"
    sent = repr(call)
    for private in ("Geheim Kundin", "0171 9999999", "KD-777", "dennis@example.com", "Dennis", "Buero"):
        assert private not in sent
    assert call["model"] == assistant.DEFAULT_MODEL


def test_nothing_is_stored_except_content_free_audit_event(app, world, enabled):
    _ask(login(app, "dennis@example.com"))
    events = _audit()
    assert len(events) == 1
    assert events[0].details == {"action": "email", "ok": True}
    assert events[0].tenant_id == world.tenant_a.id
    assert USER_TEXT not in repr([event.details for event in events])


def test_input_validation(app, world, enabled):
    client = login(app, "dennis@example.com")
    assert _ask(client, action="unbekannt").status_code == 400
    assert _ask(client, text="   ").status_code == 400
    app.config["ASSISTANT_MAX_INPUT_CHARS"] = 50
    resp = _ask(client, text="x" * 51)
    assert resp.status_code == 400
    assert "zu lang" in resp.get_json()["error"]
    assert client.post("/assistent/anfrage", data="kein json").status_code == 400
    assert enabled.beta.messages.calls == []


# --- API-Fehler -----------------------------------------------------------------------------


def _request():
    return httpx2.Request("POST", "https://api.anthropic.com/v1/messages")


def _status_error(cls, status):
    return cls("Fehler", response=httpx2.Response(status, request=_request()), body=None)


@pytest.mark.parametrize(
    ("error", "status", "error_type"),
    [
        (anthropic.APITimeoutError(request=_request()), 504, "timeout"),
        (_status_error(anthropic.RateLimitError, 429), 429, "rate_limit"),
        (anthropic.APIConnectionError(request=_request()), 502, "connection"),
        (_status_error(anthropic.AuthenticationError, 401), 502, "AuthenticationError"),
        (_status_error(anthropic.InternalServerError, 500), 502, "status_500"),
    ],
)
def test_api_errors_are_handled_without_content(app, world, enabled, caplog, error, status, error_type):
    enabled.beta.messages.behaviour = error
    with caplog.at_level(logging.DEBUG):
        resp = _ask(login(app, "dennis@example.com"))
    assert resp.status_code == status
    message = resp.get_json()["error"]
    assert message and "Fehler" not in message and SECRET_KEY not in message
    assert USER_TEXT not in caplog.text and SECRET_KEY not in caplog.text
    if status >= 500:
        assert _audit()[-1].details == {"action": "email", "ok": False, "error": error_type}


def test_refusal_and_empty_answer(app, world, enabled):
    client = login(app, "dennis@example.com")
    enabled.beta.messages.behaviour = _response(text="", stop_reason="refusal")
    assert _ask(client).status_code == 422
    enabled.beta.messages.behaviour = _response(text="  ")
    assert _ask(client).status_code == 502


def test_disabled_or_unconfigured_assistant(app, world, enabled):
    client = login(app, "dennis@example.com")
    app.config["ASSISTANT_ENABLED"] = False
    assert _ask(client).status_code == 503
    assert "data-assistant-toggle" not in client.get("/sprachnachrichten").get_data(as_text=True)
    app.config["ASSISTANT_ENABLED"] = True
    app.config["ANTHROPIC_API_KEY"] = None
    resp = _ask(client)
    assert resp.status_code == 503
    assert "nicht eingerichtet" in resp.get_json()["error"]
    assert enabled.beta.messages.calls == []


# --- UI und API-Key -------------------------------------------------------------------------


def test_panel_rendered_for_office_roles_without_api_key(app, world, enabled):
    for email in ("dennis@example.com", "admin-a@example.com"):
        client = login(app, email)
        html = client.get("/sprachnachrichten").get_data(as_text=True)
        assert 'data-assistant-toggle' in html and 'id="assistent"' in html
        assert "Freie Frage" in html and "Gesprächsnotiz erstellen" in html
        assert "Kurzfassung erstellen" in html
        assert SECRET_KEY not in html
        js = client.get("/static/js/assistant.js").get_data(as_text=True)
        assert SECRET_KEY not in js and "anthropic" not in js.lower()


def test_api_key_never_reaches_any_page(app, world, enabled):
    pages = {
        "admin-a@example.com": ["/", "/sprachnachrichten", "/hochladen", "/customers", "/search?q=Kunde"],
        "dennis@example.com": ["/", "/sprachnachrichten", "/hochladen"],
        "justin@example.com": ["/plattform", "/plattform/system"],
    }
    for email, paths in pages.items():
        client = login(app, email)
        for path in paths:
            resp = client.get(path, follow_redirects=True)
            assert resp.status_code == 200, (email, path)
            assert SECRET_KEY not in resp.get_data(as_text=True), (email, path)


def test_memo_summary_only_sends_on_explicit_request(app, world, enabled, monkeypatch):
    """Die Transkription loest nie eine KI-Anfrage aus; die Kurzfassung nur auf Klick."""
    import io

    monkeypatch.setattr("app.blueprints.dashboard.routes.transcribe_audio", lambda filename, content: USER_TEXT)
    client = login(app, "dennis@example.com")
    resp = client.post(
        "/sprachnachrichten/transkribieren",
        data={"file": (io.BytesIO(b"ID3audio"), "anruf.mp3")},
        content_type="multipart/form-data",
        headers={"Accept": "application/json"},
    )
    assert resp.status_code == 200
    client.post("/sprachnachrichten/kundenabgleich", json={"transcript": USER_TEXT, "token": resp.get_json()["token"]})
    assert enabled.beta.messages.calls == []
    assert _ask(client, action="memo_kurzfassung").status_code == 200
    assert len(enabled.beta.messages.calls) == 1


# --- Plattform-Panel ------------------------------------------------------------------------


def test_platform_panel_shows_only_technical_status(app, world, enabled):
    _ask(login(app, "dennis@example.com"))
    enabled.beta.messages.behaviour = anthropic.APITimeoutError(request=_request())
    _ask(login(app, "bob@example.com"))

    client = login(app, "justin@example.com")
    html = client.get("/plattform/system").get_data(as_text=True)
    assert "KI-Assistent" in html and "aktiviert" in html
    assert assistant.DEFAULT_MODEL in html
    assert "1 / 1" in html  # Anfragen 24 h / 30 Tage
    assert "timeout" in html
    assert USER_TEXT not in html and SECRET_KEY not in html

    resp = client.post("/plattform/system/ki-pruefen", follow_redirects=True)
    assert "KI-Assistent: erreichbar." in resp.get_data(as_text=True)
    assert len(enabled.beta.messages.calls) == 2  # die Pruefung sendet keinen Text


def test_platform_check_is_super_admin_only(app, world, enabled):
    assert login(app, "admin-a@example.com").post("/plattform/system/ki-pruefen").status_code == 403
