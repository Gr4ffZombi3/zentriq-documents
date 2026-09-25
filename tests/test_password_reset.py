import time

import pytest

from app.models.audit_log import AuditEventType, AuditLog
from app.services.password_reset import build_reset_url, generate_reset_token, verify_reset_token


@pytest.fixture()
def sent_mails(monkeypatch, app):
    """Faengt den SMTP-Versand ab und aktiviert eine (fiktive) Mail-Konfiguration."""
    app.config["SMTP_HOST"] = "smtp.example.test"
    app.config["MAIL_FROM"] = "noreply@example.test"
    mails = []
    monkeypatch.setattr(
        "app.tasks.auth_tasks.send_email", lambda to, subject, body: mails.append((to, subject, body))
    )
    return mails


def _token_from_mail(body):
    prefix = "https://zentriq.test/auth/reset-password/"
    line = next(line for line in body.splitlines() if line.startswith(prefix))
    return line[len(prefix):]


def _reset_events(event_type):
    return AuditLog.query.filter_by(event_type=event_type).all()


def test_login_page_links_to_forgot_password(client):
    html = client.get("/auth/login").get_data(as_text=True)
    assert "/auth/forgot-password" in html


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


def test_reset_link_uses_configured_public_url_not_host_header(client, user, sent_mails):
    client.post("/auth/forgot-password", data={"email": user.email}, headers={"Host": "evil.example"})
    body = sent_mails[0][2]
    assert "evil.example" not in body
    assert "https://zentriq.test/auth/reset-password/" in body


def test_forgot_password_response_identical_for_unknown_email(client, user, sent_mails):
    known = client.post("/auth/forgot-password", data={"email": user.email}, follow_redirects=True)
    unknown = client.post("/auth/forgot-password", data={"email": "niemand@example.com"}, follow_redirects=True)
    assert known.status_code == unknown.status_code == 200
    assert password_reset_message() in known.get_data(as_text=True)
    assert password_reset_message() in unknown.get_data(as_text=True)
    assert len(sent_mails) == 1


def password_reset_message():
    from app.auth.routes import RESET_REQUESTED_MESSAGE

    return RESET_REQUESTED_MESSAGE.split(",")[0]


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


def test_forgot_password_without_mail_config_sends_nothing(client, user, monkeypatch):
    calls = []
    monkeypatch.setattr("app.tasks.auth_tasks.send_email", lambda *args: calls.append(args))
    resp = client.post("/auth/forgot-password", data={"email": user.email})
    assert resp.status_code == 302
    assert calls == []


def test_forgot_password_survives_broker_failure(client, user, monkeypatch):
    def broken_delay(*args, **kwargs):
        raise ConnectionError("broker down")

    monkeypatch.setattr("app.auth.routes.send_password_reset_email.delay", broken_delay)
    resp = client.post("/auth/forgot-password", data={"email": user.email})
    assert resp.status_code == 302


def test_reset_password_sets_new_password_and_token_is_single_use(client, user):
    token = generate_reset_token(user)

    page = client.get(f"/auth/reset-password/{token}")
    assert page.status_code == 200
    assert page.headers["Referrer-Policy"] == "no-referrer"
    assert "no-store" in page.headers["Cache-Control"]

    resp = client.post(
        f"/auth/reset-password/{token}",
        data={"password": "ganz-neues-passwort", "password_confirm": "ganz-neues-passwort"},
    )
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("/auth/login")
    assert user.check_password("ganz-neues-passwort")
    assert not user.check_password("testpassword123")
    assert len(_reset_events(AuditEventType.PASSWORD_RESET_COMPLETED)) == 1

    # Zweite Verwendung desselben Tokens scheitert, weil sich der Passwort-Hash geaendert hat.
    reuse = client.post(
        f"/auth/reset-password/{token}",
        data={"password": "noch-ein-passwort", "password_confirm": "noch-ein-passwort"},
    )
    assert reuse.status_code == 302
    assert reuse.headers["Location"].endswith("/auth/forgot-password")
    assert user.check_password("ganz-neues-passwort")

    login = client.post(
        "/auth/login",
        data={"login_type": "email", "identifier": user.email, "password": "ganz-neues-passwort"},
    )
    assert login.status_code == 302


def test_reset_password_validates_confirmation(client, user):
    token = generate_reset_token(user)
    resp = client.post(
        f"/auth/reset-password/{token}", data={"password": "ganz-neues-passwort", "password_confirm": "anders123"}
    )
    assert resp.status_code == 200
    assert "stimmen nicht überein" in resp.get_data(as_text=True)
    assert user.check_password("testpassword123")


@pytest.mark.parametrize("token", ["kaputt", "eyJ1aWQiOjF9.invalid.signature"])
def test_reset_password_rejects_invalid_token(client, user, token):
    resp = client.get(f"/auth/reset-password/{token}")
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("/auth/forgot-password")


def test_reset_token_expires(app, user, monkeypatch):
    token = generate_reset_token(user)
    app.config["PASSWORD_RESET_TOKEN_MAX_AGE_SECONDS"] = 60
    monkeypatch.setattr(
        "itsdangerous.timed.TimestampSigner.get_timestamp", lambda self: int(time.time()) + 3600
    )
    assert verify_reset_token(token) is None


def test_reset_token_signed_with_other_key_is_rejected(app, user):
    token = generate_reset_token(user)
    app.config["SECRET_KEY"] = "ein-anderer-schluessel"
    assert verify_reset_token(token) is None


def test_reset_token_rejected_for_inactive_user(db, user):
    token = generate_reset_token(user)
    user.is_active = False
    db.session.commit()
    assert verify_reset_token(token) is None


def test_build_reset_url_requires_public_url(app):
    app.config["PUBLIC_URL"] = ""
    with pytest.raises(RuntimeError):
        build_reset_url("abc")


def test_reset_url_matches_route(app, client, user):
    url = build_reset_url(generate_reset_token(user))
    path = url.removeprefix(app.config["PUBLIC_URL"])
    assert client.get(path).status_code == 200
