"""Sitzung, Sicherheitstoken und Wiederholungen ohne Duplikate (app/services/idempotency.py,
GET /auth/sitzung, GET /auth/aktion/<key>, js/session-guard.js)."""

import io
from datetime import timedelta

import pytest
from flask import send_file

from app.models import CalendarEvent, RequestReceipt
from app.services import idempotency
from app.tenancy import bypass_tenant_scope
from config import BaseConfig, TestingConfig

API = "/kalender/api"
KEY = "testkey0123456789abcdef"


def _create(client, assignee_id, key=None, **extra):
    body = {"title": "Beratung", "start": "2026-11-12T14:00", "end": "2026-11-12T14:30", "assignee_id": assignee_id}
    body.update(extra)
    headers = {"Idempotency-Key": key} if key else {}
    return client.post(f"{API}/termine", json=body, headers=headers)


def _event_count():
    with bypass_tenant_scope():
        return CalendarEvent.query.count()


def test_csrf_token_lives_as_long_as_the_session():
    # Vorher Flask-WTF-Standard 3600 s bei 8 h Sitzung -> "Seite veraltet" nach einer Stunde.
    assert BaseConfig.WTF_CSRF_TIME_LIMIT == int(BaseConfig.PERMANENT_SESSION_LIFETIME.total_seconds())
    assert BaseConfig.WTF_CSRF_TIME_LIMIT is not None  # Pruefung bleibt zeitlich begrenzt


def test_session_state_without_and_with_login(client, auth_client, user):
    anon = client.application.test_client().get("/auth/sitzung")
    assert anon.status_code == 200
    data = anon.get_json()
    assert data["authenticated"] is False and data["user_id"] is None
    assert anon.headers["Cache-Control"] == "no-store"

    data = auth_client.get("/auth/sitzung").get_json()
    assert data["authenticated"] is True and data["user_id"] == user.id
    assert data["csrf_token"]


def test_user_meta_and_guard_script_on_pages(auth_client, user):
    html = auth_client.get("/kalender").get_data(as_text=True)
    assert f'<meta name="zq-user" content="{user.id}">' in html
    assert "js/session-guard.js" in html
    # Vor htmx/app.js geladen, damit fetch schon beim ersten Aufruf geschuetzt ist.
    assert html.index("js/session-guard.js") < html.index("js/app.js")


def test_login_page_has_guard_but_no_user_meta(client):
    html = client.get("/auth/login").get_data(as_text=True)
    assert "js/session-guard.js" in html
    assert 'name="zq-user"' not in html


def test_repeated_create_with_same_key_saves_once(auth_client, user):
    first = _create(auth_client, user.id, key=KEY)
    assert first.status_code == 201
    second = _create(auth_client, user.id, key=KEY)
    assert second.status_code == 201
    assert second.headers.get("Idempotent-Replay") == "true"
    assert second.get_json()["event"]["id"] == first.get_json()["event"]["id"]
    assert _event_count() == 1


def test_different_keys_are_different_actions(auth_client, user):
    assert _create(auth_client, user.id, key=KEY).status_code == 201
    assert _create(auth_client, user.id, key=KEY + "2", start="2026-11-12T15:00", end="2026-11-12T15:30").status_code == 201
    assert _event_count() == 2


def test_without_key_behaviour_is_unchanged(auth_client, user):
    assert _create(auth_client, user.id).status_code == 201
    assert _create(auth_client, user.id, start="2026-11-12T16:00", end="2026-11-12T16:30").status_code == 201
    with bypass_tenant_scope():
        assert RequestReceipt.query.count() == 0


def test_failed_attempt_is_not_stored_and_may_be_retried(auth_client, user):
    bad = _create(auth_client, user.id, key=KEY, start="kaputt")
    assert bad.status_code >= 400
    with bypass_tenant_scope():
        assert RequestReceipt.query.count() == 0
    ok = _create(auth_client, user.id, key=KEY)
    assert ok.status_code == 201
    assert ok.headers.get("Idempotent-Replay") is None


def test_key_of_another_user_is_independent(app, auth_client, user, employee):
    other = app.test_client()
    other.post("/auth/login", data={"login_type": "email", "identifier": "mitarbeiter@example.com", "password": "mitarbeiterpass123"})
    assert _create(auth_client, user.id, key=KEY).status_code == 201
    resp = _create(other, employee.id, key=KEY, start="2026-11-12T09:00", end="2026-11-12T09:30")
    assert resp.status_code == 201
    assert resp.headers.get("Idempotent-Replay") is None
    assert _event_count() == 2
    # Fremde Quittungen sind nicht abfragbar.
    assert other.get(f"/auth/aktion/{KEY}").get_json()["status"] == "done"  # eigene Aktion
    with bypass_tenant_scope():
        assert RequestReceipt.query.filter_by(key=KEY).count() == 2


def test_key_reused_for_other_endpoint_is_rejected(auth_client, user):
    assert _create(auth_client, user.id, key=KEY).status_code == 201
    resp = auth_client.post("/arbeitsplatz/listendichte", json={"density": "kompakt"}, headers={"Idempotency-Key": KEY})
    assert resp.status_code == 422
    assert resp.get_json()["code"] == "idempotency_mismatch"


def test_parallel_duplicate_while_pending(auth_client, user, db):
    with bypass_tenant_scope():
        db.session.add(RequestReceipt(user_id=user.id, key=KEY, method="POST", path=f"{API}/termine", state="pending"))
        db.session.commit()
    resp = _create(auth_client, user.id, key=KEY)
    assert resp.status_code == 409
    assert resp.get_json()["code"] == "in_progress"
    assert _event_count() == 0
    assert auth_client.get(f"/auth/aktion/{KEY}").get_json()["status"] == "pending"


def test_stale_pending_attempt_is_executed_again(auth_client, user, db):
    with bypass_tenant_scope():
        receipt = RequestReceipt(user_id=user.id, key=KEY, method="POST", path=f"{API}/termine", state="pending")
        db.session.add(receipt)
        db.session.commit()
        receipt.created_at = receipt.created_at - idempotency.STALE_PENDING - timedelta(seconds=1)
        db.session.commit()
    assert auth_client.get(f"/auth/aktion/{KEY}").get_json()["status"] == "unknown"
    assert _create(auth_client, user.id, key=KEY).status_code == 201
    assert _event_count() == 1


def test_action_state_endpoint(auth_client, user):
    assert auth_client.get(f"/auth/aktion/{KEY}").get_json() == {"status": "unknown"}
    _create(auth_client, user.id, key=KEY)
    assert auth_client.get(f"/auth/aktion/{KEY}").get_json() == {"status": "done", "status_code": 201}
    assert auth_client.get("/auth/aktion/x").get_json() == {"status": "unknown"}


def test_action_state_requires_login(client):
    resp = client.get(f"/auth/aktion/{KEY}", headers={"Accept": "application/json"})
    assert resp.status_code == 401
    assert resp.get_json()["code"] == "login_required"


def test_classic_form_field_is_accepted(auth_client, user):
    data = {"density": "kompakt", "_idem": KEY}
    first = auth_client.post("/arbeitsplatz/listendichte", data=data)
    second = auth_client.post("/arbeitsplatz/listendichte", data=data)
    assert first.status_code == second.status_code
    assert second.headers.get("Idempotent-Replay") == "true"


def test_old_receipts_are_purged(auth_client, user, db):
    with bypass_tenant_scope():
        old = RequestReceipt(user_id=user.id, key="alt0123456789", method="POST", path="/x", state="done", status_code=200)
        db.session.add(old)
        db.session.commit()
        old.created_at = old.created_at - idempotency.RETENTION - timedelta(minutes=1)
        db.session.commit()
    _create(auth_client, user.id, key=KEY)
    with bypass_tenant_scope():
        assert RequestReceipt.query.filter_by(key="alt0123456789").count() == 0


def test_fetch_marker_gets_json_for_expired_login(client):
    resp = client.post(f"{API}/termine", json={}, headers={"X-Zentriq-Fetch": "1"})
    assert resp.status_code == 401
    assert resp.get_json()["code"] == "login_required"


@pytest.fixture()
def csrf_app(tmp_path):
    from app import create_app
    from app.extensions import db as _db

    class CsrfConfig(TestingConfig):
        WTF_CSRF_ENABLED = True

    application = create_app(CsrfConfig)
    application.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{tmp_path / 'csrf.db'}"
    with application.app_context():
        _db.create_all()
        yield application
        _db.session.remove()
        _db.drop_all()


def test_stale_login_form_is_shown_again_with_identifier(csrf_app):
    client = csrf_app.test_client()
    resp = client.post("/auth/login", data={"login_type": "email", "identifier": "laura@example.com", "password": "x",
                                            "csrf_token": "veraltet"})
    assert resp.status_code == 400
    html = resp.get_data(as_text=True)
    assert "zu lange geöffnet" in html
    assert 'value="laura@example.com"' in html
    assert 'name="csrf_token"' in html
    assert 'value="x"' not in html  # Passwort nie vorbelegt


def test_csrf_error_for_fetch_is_json(csrf_app):
    client = csrf_app.test_client()
    resp = client.post("/arbeitsplatz/listendichte", json={}, headers={"X-Zentriq-Fetch": "1", "X-CSRFToken": "veraltet"})
    assert resp.status_code == 400
    assert resp.get_json()["code"] == "csrf"


def test_fresh_token_from_session_state_is_accepted(csrf_app):
    client = csrf_app.test_client()
    token = client.get("/auth/sitzung").get_json()["csrf_token"]
    resp = client.post("/auth/login", data={"login_type": "email", "identifier": "niemand@example.com",
                                            "password": "falsch", "csrf_token": token})
    # Token gilt: normale Anmeldepruefung statt "Formular zu lange geoeffnet".
    assert "zu lange geöffnet" not in resp.get_data(as_text=True)


def test_file_responses_are_not_stored(app, user):
    @app.post("/_test/datei")
    def _datei():
        return send_file(io.BytesIO(b"%PDF-1.4"), mimetype="application/pdf", download_name="x.pdf")

    client = app.test_client()
    client.post("/auth/login", data={"login_type": "email", "identifier": user.email, "password": "testpassword123"})
    resp = client.post("/_test/datei", headers={"Idempotency-Key": KEY})
    assert resp.status_code == 200
    with bypass_tenant_scope():
        assert RequestReceipt.query.filter_by(key=KEY).count() == 0
