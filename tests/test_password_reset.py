import time
from urllib.parse import urlsplit

import pytest

from app.models.audit_log import AuditEventType, AuditLog
from app.services.password_reset import build_reset_url, generate_reset_token, verify_reset_token

RESET_PATH = "/auth/reset-password"
LINK_PREFIX = "https://zentriq.test/auth/reset-password#token="


@pytest.fixture(autouse=True)
def sent_mails(monkeypatch, app):
    """Faengt den SMTP-Versand ab und aktiviert eine (fiktive) Mail-Konfiguration - ohne sie
    ist der Passwort-Reset bewusst nicht verfuegbar (siehe Tests weiter unten)."""
    app.config["SMTP_HOST"] = "smtp.example.test"
    app.config["MAIL_FROM"] = "noreply@example.test"
    mails = []
    monkeypatch.setattr(
        "app.tasks.auth_tasks.send_email", lambda to, subject, body: mails.append((to, subject, body))
    )
    return mails


def _token_from_mail(body):
    line = next(line for line in body.splitlines() if line.startswith(LINK_PREFIX))
    return line[len(LINK_PREFIX):]


def _reset_events(event_type):
    return AuditLog.query.filter_by(event_type=event_type).all()


def _submit_reset(client, token, password, confirm=None):
    return client.post(
        RESET_PATH,
        data={"token": token, "password": password, "password_confirm": confirm or password},
    )


def _expire_tokens(app, monkeypatch):
    app.config["PASSWORD_RESET_TOKEN_MAX_AGE_SECONDS"] = 60
    monkeypatch.setattr(
        "itsdangerous.timed.TimestampSigner.get_timestamp", lambda self: int(time.time()) + 3600
    )


def password_reset_message():
    from app.auth.routes import RESET_REQUESTED_MESSAGE

    return RESET_REQUESTED_MESSAGE.split(",")[0]


def test_login_page_links_to_forgot_password(client):
    html = client.get("/auth/login").get_data(as_text=True)
    assert "/auth/forgot-password" in html
    assert "Passwort vergessen?" in html


def test_forgot_password_sends_mail_with_working_link(client, user, sent_mails):
    resp = client.post("/auth/forgot-password", data={"email": "TEST@example.com "})
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("/auth/login")

    assert len(sent_mails) == 1
    to, _subject, body = sent_mails[0]
    assert to == user.email
    token = _token_from_mail(body)
    assert verify_reset_token(token).id == user.id
    assert len(_reset_events(AuditEventType.PASSWORD_RESET_REQUESTED)) == 1


def test_reset_link_keeps_token_out_of_server_visible_url(client, user, sent_mails):
    """Das Token steht nur im Fragment - Browser senden es nicht mit, es landet also weder
    in Access-Logs noch im Referer."""
    client.post("/auth/forgot-password", data={"email": user.email})
    link = next(line for line in sent_mails[0][2].splitlines() if line.startswith("https://"))
    parts = urlsplit(link)
    assert parts.path == RESET_PATH
    assert parts.query == ""
    assert parts.fragment.startswith("token=")


def test_reset_link_uses_configured_public_url_not_host_header(client, user, sent_mails):
    client.post("/auth/forgot-password", data={"email": user.email}, headers={"Host": "evil.example"})
    body = sent_mails[0][2]
    assert "evil.example" not in body
    assert LINK_PREFIX in body


def test_forgot_password_response_identical_for_unknown_email(client, user, sent_mails):
    known = client.post("/auth/forgot-password", data={"email": user.email}, follow_redirects=True)
    unknown = client.post("/auth/forgot-password", data={"email": "niemand@example.com"}, follow_redirects=True)
    assert known.status_code == unknown.status_code == 200
    assert password_reset_message() in known.get_data(as_text=True)
    assert password_reset_message() in unknown.get_data(as_text=True)
    assert len(sent_mails) == 1


def test_forgot_password_ignores_inactive_user(client, db, user, sent_mails):
    user.is_active = False
    db.session.commit()
    resp = client.post("/auth/forgot-password", data={"email": user.email})
    assert resp.status_code == 302
    assert sent_mails == []


def test_forgot_password_is_rate_limited_per_account(client, app, user, sent_mails):
    app.config["PASSWORD_RESET_MAX_REQUESTS_PER_HOUR"] = 2
    for _ in range(4):
        resp = client.post("/auth/forgot-password", data={"email": user.email})
        assert resp.status_code == 302
    assert len(sent_mails) == 2
    assert len(_reset_events(AuditEventType.PASSWORD_RESET_REQUESTED)) == 2


@pytest.mark.parametrize("missing", ["SMTP_HOST", "MAIL_FROM", "PUBLIC_URL"])
def test_reset_is_unavailable_without_complete_mail_config(app, client, user, sent_mails, missing):
    app.config[missing] = None
    login_html = client.get("/auth/login")
    assert login_html.status_code == 200
    assert "Passwort vergessen?" not in login_html.get_data(as_text=True)
    assert client.get("/auth/forgot-password").status_code == 404
    assert client.post("/auth/forgot-password", data={"email": user.email}).status_code == 404
    assert client.get(RESET_PATH).status_code == 404
    assert _submit_reset(client, generate_reset_token(user), "ganz-neues-passwort").status_code == 404
    assert user.check_password("testpassword123")
    assert sent_mails == []
    assert _reset_events(AuditEventType.PASSWORD_RESET_REQUESTED) == []


def test_login_template_only_builds_reset_url_when_available(app):
    """Hotfix-Absicherung: login.html ruft url_for('auth.forgot_password') nur auf, wenn
    password_reset_available gesetzt ist - ein Prozess ohne diese Variable (z. B. ein noch
    laufender Altstand ohne Reset-Route) rendert die Seite daher ohne BuildError."""
    source = app.jinja_loader.get_source(app.jinja_env, "auth/login.html")[0]
    guard = source.index("{% if password_reset_available %}")
    assert guard < source.index("url_for('auth.forgot_password')") < source.index("{% endif %}", guard)


def test_send_task_is_fail_closed_without_mail_config(app, user, sent_mails):
    from app.tasks.auth_tasks import send_password_reset_email

    app.config["SMTP_HOST"] = None
    assert send_password_reset_email(user.id) == {"sent": False, "reason": "not_configured"}
    assert sent_mails == []


def test_forgot_password_survives_broker_failure(client, user, monkeypatch):
    def broken_delay(*args, **kwargs):
        raise ConnectionError("broker down")

    monkeypatch.setattr("app.auth.routes.send_password_reset_email.delay", broken_delay)
    resp = client.post("/auth/forgot-password", data={"email": user.email})
    assert resp.status_code == 302


def test_reset_page_renders_with_hardening_headers(client):
    page = client.get(RESET_PATH)
    assert page.status_code == 200
    assert page.headers["Referrer-Policy"] == "no-referrer"
    assert "no-store" in page.headers["Cache-Control"]
    html = page.get_data(as_text=True)
    assert 'name="token"' in html
    assert "history.replaceState" in html


def test_valid_reset_changes_password_and_token_is_single_use(client, user):
    token = generate_reset_token(user)

    resp = _submit_reset(client, token, "ganz-neues-passwort")
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("/auth/login")
    assert user.check_password("ganz-neues-passwort")
    assert not user.check_password("testpassword123")
    assert len(_reset_events(AuditEventType.PASSWORD_RESET_COMPLETED)) == 1

    # Zweite Verwendung desselben Tokens scheitert, weil sich der Passwort-Hash geaendert hat.
    reuse = _submit_reset(client, token, "noch-ein-passwort")
    assert reuse.status_code == 302
    assert reuse.headers["Location"].endswith("/auth/forgot-password")
    assert user.check_password("ganz-neues-passwort")


def test_login_works_with_new_password_after_reset(client, user):
    _submit_reset(client, generate_reset_token(user), "ganz-neues-passwort")

    old = client.post(
        "/auth/login",
        data={"login_type": "email", "identifier": user.email, "password": "testpassword123"},
    )
    assert old.status_code == 200
    assert "Anmeldedaten sind falsch" in old.get_data(as_text=True)

    new = client.post(
        "/auth/login",
        data={"login_type": "email", "identifier": user.email, "password": "ganz-neues-passwort"},
    )
    assert new.status_code == 302
    assert client.get("/").status_code == 200


def test_reset_password_mismatch_is_rejected(client, user):
    token = generate_reset_token(user)
    resp = _submit_reset(client, token, "ganz-neues-passwort", "anders12345")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "stimmen nicht überein" in html
    assert user.check_password("testpassword123")


def test_reset_password_too_short_is_rejected(client, user):
    resp = _submit_reset(client, generate_reset_token(user), "kurz")
    assert resp.status_code == 200
    assert "Mindestens 8 Zeichen" in resp.get_data(as_text=True)
    assert user.check_password("testpassword123")


@pytest.mark.parametrize("token", ["", "kaputt", "eyJ1aWQiOjF9.invalid.signature"])
def test_reset_password_rejects_invalid_token(client, user, token):
    resp = _submit_reset(client, token, "ganz-neues-passwort")
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("/auth/forgot-password")
    assert user.check_password("testpassword123")
    follow = client.get(resp.headers["Location"])
    assert "ungültig oder abgelaufen" in follow.get_data(as_text=True)


def test_reset_password_rejects_expired_token(app, client, user, monkeypatch):
    token = generate_reset_token(user)
    _expire_tokens(app, monkeypatch)
    resp = _submit_reset(client, token, "ganz-neues-passwort")
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("/auth/forgot-password")
    assert user.check_password("testpassword123")


def test_reset_token_expires(app, user, monkeypatch):
    token = generate_reset_token(user)
    _expire_tokens(app, monkeypatch)
    assert verify_reset_token(token) is None


def test_reset_token_signed_with_other_key_is_rejected(app, user):
    token = generate_reset_token(user)
    app.config["SECRET_KEY"] = "ein-anderer-schluessel"
    assert verify_reset_token(token) is None


def test_reset_token_is_not_valid_for_other_purposes(app, user):
    """Andere Signaturen mit demselben SECRET_KEY (z. B. Session/CSRF) haben einen anderen
    Salt und werden nicht als Reset-Token akzeptiert."""
    from itsdangerous import URLSafeTimedSerializer

    foreign = URLSafeTimedSerializer(app.config["SECRET_KEY"]).dumps({"uid": user.id})
    assert verify_reset_token(foreign) is None


def test_reset_token_rejected_for_inactive_user(db, user):
    token = generate_reset_token(user)
    user.is_active = False
    db.session.commit()
    assert verify_reset_token(token) is None


def test_reset_token_contains_no_password_data(user):
    import base64
    import json

    payload_part = generate_reset_token(user).split(".")[0]
    payload = json.loads(base64.urlsafe_b64decode(payload_part + "=" * (-len(payload_part) % 4)))
    assert set(payload) == {"uid", "pw"}
    assert payload["pw"] not in user.password_hash


def test_build_reset_url_requires_public_url(app):
    app.config["PUBLIC_URL"] = ""
    with pytest.raises(RuntimeError):
        build_reset_url("abc")
