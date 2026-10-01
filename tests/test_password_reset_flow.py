"""Passwort vergessen - produktionsreifer Ablauf: Empfaenger ist immer die im Konto hinterlegte
Login-E-Mail, Versand nur mit eingerichteter 2FA, Token zeitlich begrenzt und einmalig,
identische Antwort fuer alle Faelle, Mandantentrennung, SMTP-Fehler."""

import logging
import smtplib
import time

import pytest

from app.extensions import db
from app.models import AuditLog, Tenant, TenantStatus, User, UserRole
from app.models.audit_log import AuditEventType
from app.services.password_reset import generate_reset_token, verify_reset_token
from app.tenancy import bypass_tenant_scope, use_tenant_id
from tests.two_factor_helpers import enable_two_factor, totp_code

LINK_PREFIX = "https://zentriq.test/auth/reset-password#token="
GENERIC = "Wenn für diese E-Mail-Adresse ein Konto existiert"
PASSWORD = "altes-passwort-1"


@pytest.fixture()
def smtp(app, monkeypatch):
    """Ersetzt nur die SMTP-Verbindung (kein echter Versand) - send_email selbst laeuft echt,
    damit Absender/Empfaenger der tatsaechlich gebauten Nachricht geprueft werden."""
    app.config.update(SMTP_HOST="smtp.example.test", MAIL_FROM="Zentriq <noreply@zentriq.test>", SMTP_USERNAME="smtp-user", SMTP_PASSWORD="smtp-geheim")
    class Outbox(list):
        fake = None

    sent = Outbox()

    class FakeSMTP:
        fail = False

        def __init__(self, host, port, timeout=None):
            self.host = host

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def starttls(self, context=None):
            pass

        def login(self, username, password):
            pass

        def send_message(self, message):
            if FakeSMTP.fail:
                raise smtplib.SMTPAuthenticationError(535, b"Authentication failed for smtp-geheim")
            sent.append(message)

    monkeypatch.setattr("app.services.mailer.smtplib.SMTP", FakeSMTP)
    sent.fake = FakeSMTP
    return sent


def _make(tenant, email, role=UserRole.EMPLOYEE, vm=None, two_factor=True):
    with use_tenant_id(tenant.id):
        user = User(tenant_id=tenant.id, email=email, role=role, vermittlernummer=vm, is_active=True)
        user.set_password(PASSWORD)
        db.session.add(user)
        db.session.commit()
    secret = enable_two_factor(user)[0] if two_factor else None
    return user, secret


def _reload(user_id):
    db.session.expire_all()
    with bypass_tenant_scope():
        return db.session.get(User, user_id)


def _forgot(client, email, **extra):
    return client.post("/auth/forgot-password", data={"email": email, **extra}, follow_redirects=True)


def _token(message):
    body = message.get_content()
    line = next(line for line in body.splitlines() if line.startswith(LINK_PREFIX))
    return line[len(LINK_PREFIX):]


def _reset(client, token, code, password="neues-passwort-9"):
    return client.post(
        "/auth/reset-password",
        data={"token": token, "code": code, "password": password, "password_confirm": password},
    )


# --- Erfolgreicher Reset -----------------------------------------------------------------------


def test_successful_reset_mails_stored_login_address(client, tenant, smtp):
    user, secret = _make(tenant, "dennis@example.com", vm="08/1234-A")
    resp = _forgot(client, "  DENNIS@Example.com ")
    assert GENERIC in resp.get_data(as_text=True)

    assert len(smtp) == 1
    message = smtp[0]
    assert message["To"] == "dennis@example.com"  # aus der DB, nicht die Request-Schreibweise
    assert message["From"] == "Zentriq <noreply@zentriq.test>"
    body = message.get_content()
    assert LINK_PREFIX in body
    assert "Authenticator-App" in body and "Mit freundlichen Grüßen" in body
    assert PASSWORD not in body and "smtp-geheim" not in body

    assert _reset(client, _token(message), totp_code(user, secret)).status_code == 302
    assert _reload(user.id).check_password("neues-passwort-9")


def test_request_cannot_inject_recipient(client, tenant, smtp):
    _make(tenant, "dennis@example.com")
    _forgot(client, "dennis@example.com", to="angreifer@evil.test", recipient="angreifer@evil.test")
    _forgot(client, "dennis@example.com\nBcc: angreifer@evil.test")
    assert [m["To"] for m in smtp] == ["dennis@example.com"]
    assert all("evil.test" not in str(m) for m in smtp)


def test_reset_changes_only_password(client, tenant, smtp):
    user, secret = _make(tenant, "chefin@example.com", role=UserRole.OFFICE_ADMIN, vm="08/0001-A")
    before = _reload(user.id)
    version_before = before.auth_version
    snapshot = (before.role, before.tenant_id, before.vermittlernummer, before.email, before.is_active, before.totp_enabled_at)
    _forgot(client, "chefin@example.com")
    assert _reset(client, _token(smtp[0]), totp_code(user, secret)).status_code == 302
    after = _reload(user.id)
    assert (after.role, after.tenant_id, after.vermittlernummer, after.email, after.is_active, after.totp_enabled_at) == snapshot
    assert after.auth_version == version_before + 1  # nur Sessions beendet
    assert AuditLog.query.filter_by(event_type=AuditEventType.PASSWORD_RESET_COMPLETED).count() == 1


# --- Kein Versand, identische Antwort --------------------------------------------------------


def test_no_mail_without_two_factor(client, tenant, smtp):
    user, _ = _make(tenant, "ohne2fa@example.com", two_factor=False)
    unknown = _forgot(client, "niemand@example.com").get_data(as_text=True)
    resp = _forgot(client, "ohne2fa@example.com").get_data(as_text=True)
    assert GENERIC in resp and GENERIC in unknown
    assert smtp == []
    entry = AuditLog.query.filter_by(actor_user_id=user.id, event_type=AuditEventType.PASSWORD_RESET_REQUESTED).one()
    assert entry.details["reason"] == "two_factor_missing"


@pytest.mark.parametrize("state", ["unknown", "inactive", "deleted", "tenant_suspended"])
def test_unknown_or_disabled_accounts_get_identical_answer_and_no_mail(client, tenant, smtp, state):
    from datetime import datetime

    user, _ = _make(tenant, "konto@example.com")
    if state == "inactive":
        user.is_active = False
    elif state == "deleted":
        user.is_active, user.deleted_at = False, datetime(2026, 1, 1)
    elif state == "tenant_suspended":
        tenant.status = TenantStatus.SUSPENDED
    db.session.commit()
    email = "unbekannt@example.com" if state == "unknown" else "konto@example.com"
    resp = _forgot(client, email)
    assert resp.status_code == 200 and GENERIC in resp.get_data(as_text=True)
    assert smtp == []


# --- Token ---------------------------------------------------------------------------------------


def test_invalid_token_rejected(client, tenant, smtp):
    user, secret = _make(tenant, "konto@example.com")
    resp = _reset(client, "manipuliert.token.xyz", totp_code(user, secret))
    assert resp.headers["Location"].endswith("/auth/forgot-password")
    assert _reload(user.id).check_password(PASSWORD)


def test_expired_token_rejected(app, client, tenant, smtp, monkeypatch):
    user, secret = _make(tenant, "konto@example.com")
    token = generate_reset_token(user)
    app.config["PASSWORD_RESET_TOKEN_MAX_AGE_SECONDS"] = 60
    monkeypatch.setattr("itsdangerous.timed.TimestampSigner.get_timestamp", lambda self: int(time.time()) + 3600)
    assert _reset(client, token, totp_code(user, secret)).headers["Location"].endswith("/auth/forgot-password")
    assert _reload(user.id).check_password(PASSWORD)


def test_used_token_rejected(client, tenant, smtp):
    user, secret = _make(tenant, "konto@example.com")
    _forgot(client, "konto@example.com")
    token = _token(smtp[0])
    assert _reset(client, token, totp_code(user, secret)).status_code == 302
    again = _reset(client, token, totp_code(user, secret), "drittes-passwort-1")
    assert again.headers["Location"].endswith("/auth/forgot-password")
    assert _reload(user.id).check_password("neues-passwort-9")


def test_token_invalid_after_tenant_suspended(client, tenant, smtp):
    user, secret = _make(tenant, "konto@example.com")
    token = generate_reset_token(user)
    tenant.status = TenantStatus.SUSPENDED
    db.session.commit()
    assert verify_reset_token(token) is None
    assert _reload(user.id).check_password(PASSWORD)


# --- Mandantentrennung ---------------------------------------------------------------------------


def test_reset_is_isolated_between_tenants(app, client, tenant, smtp):
    office_b = Tenant(name="Buero B", slug="buero-b-reset")
    db.session.add(office_b)
    db.session.commit()
    user_a, secret_a = _make(tenant, "a@example.com", vm="08/1111-A")
    user_b, secret_b = _make(office_b, "b@example.com", vm="08/2222-B")

    _forgot(client, "b@example.com")
    assert [m["To"] for m in smtp] == ["b@example.com"]
    token_b = _token(smtp[0])
    # Code aus Konto A hilft nicht beim Token von Konto B.
    assert _reset(client, token_b, totp_code(user_a, secret_a)).status_code == 200
    assert _reset(client, token_b, totp_code(user_b, secret_b)).status_code == 302

    b, a = _reload(user_b.id), _reload(user_a.id)
    assert b.check_password("neues-passwort-9") and b.tenant_id == office_b.id
    assert a.check_password(PASSWORD) and a.tenant_id == tenant.id

    # Nach dem Reset landet B ausschliesslich im eigenen Buero.
    client.post("/auth/login", data={"login_type": "email", "identifier": "b@example.com", "password": "neues-passwort-9"})
    client.post("/auth/2fa", data={"code": totp_code(b, secret_b)})
    assert client.get("/zeiterfassung").status_code == 200
    assert client.get(f"/zeiterfassung/team/{user_a.id}").status_code == 403


def test_admin_triggered_reset_uses_target_login_address(auth_client, tenant, smtp):
    employee, _ = _make(tenant, "mitarbeiter2@example.com")
    assert auth_client.post(f"/settings/users/{employee.id}/passwort-reset").status_code == 302
    assert [m["To"] for m in smtp] == ["mitarbeiter2@example.com"]


# --- SMTP-Fehler, CSRF ---------------------------------------------------------------------------


def test_smtp_error_is_handled_without_leaking(client, tenant, smtp, caplog):
    user, _ = _make(tenant, "konto@example.com")
    smtp.fake.fail = True
    with caplog.at_level(logging.INFO):
        resp = _forgot(client, "konto@example.com")
    assert resp.status_code == 200 and GENERIC in resp.get_data(as_text=True)
    assert smtp == []
    assert "SMTPAuthenticationError" in caplog.text
    assert "smtp-geheim" not in caplog.text and "konto@example.com" not in caplog.text
    # Ausgabe des Tasks selbst.
    from app.tasks.auth_tasks import send_password_reset_email

    assert send_password_reset_email(user.id) == {"sent": False, "reason": "smtp_error"}


def test_forgot_password_requires_csrf(app, client, tenant, smtp):
    _make(tenant, "konto@example.com")
    app.config["WTF_CSRF_ENABLED"] = True
    resp = client.post("/auth/forgot-password", data={"email": "konto@example.com"})
    assert resp.status_code in (200, 400)
    assert smtp == []
