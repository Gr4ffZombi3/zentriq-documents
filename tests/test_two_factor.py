"""Zwei-Faktor-Authentifizierung (TOTP + Recovery Codes) und Passwort-Reset mit 2FA."""

import time

import pyotp
import pytest

from app.extensions import db
from app.models import AuditLog, RecoveryCode, Tenant, User, UserRole
from app.models.audit_log import AuditEventType
from app.services import two_factor
from app.services.password_reset import generate_reset_token
from app.tenancy import bypass_tenant_scope, use_tenant_id
from tests.two_factor_helpers import enable_two_factor, totp_code

PASSWORD = "testpassword123"


def _post_login(client, user, password=PASSWORD):
    return client.post("/auth/login", data={"login_type": "email", "identifier": user.email, "password": password})


@pytest.fixture()
def secured(user):
    secret, codes = enable_two_factor(user)
    return user, secret, codes


def _reload(user):
    db.session.expire_all()
    with bypass_tenant_scope():
        return db.session.get(User, user.id)


# --- Einrichtung -------------------------------------------------------------------------------


def test_setup_requires_valid_code_before_activation(auth_client, user):
    page = auth_client.get("/settings/sicherheit")
    html = page.get_data(as_text=True)
    assert page.status_code == 200
    assert "<svg" in html  # QR-Code inline, kein externer Dienst
    assert "no-store" in page.headers["Cache-Control"]
    user = _reload(user)
    assert user.totp_secret_encrypted and not user.two_factor_enabled

    assert auth_client.post("/settings/sicherheit", data={"code": "000000"}).status_code == 200
    assert _reload(user).two_factor_enabled is False

    secret = two_factor._decrypt(_reload(user).totp_secret_encrypted)
    resp = auth_client.post("/settings/sicherheit", data={"code": pyotp.TOTP(secret).now()})
    html = resp.get_data(as_text=True)
    assert _reload(user).two_factor_enabled is True
    assert "Wiederherstellungscodes" in html
    assert RecoveryCode.query.filter_by(user_id=user.id).count() == 10
    assert AuditLog.query.filter_by(event_type=AuditEventType.TWO_FACTOR_ENABLED).count() == 1


def test_secret_and_recovery_codes_are_not_stored_in_plaintext(secured):
    user, secret, codes = secured
    user = _reload(user)
    assert secret not in user.totp_secret_encrypted
    stored = {row.code_hash for row in RecoveryCode.query.filter_by(user_id=user.id)}
    assert not any(code in stored or code.replace("-", "") in stored for code in codes)


def test_recovery_codes_shown_only_once(auth_client, secured):
    _user, _secret, codes = secured
    html = auth_client.get("/settings/sicherheit").get_data(as_text=True)
    assert not any(code in html for code in codes)


# --- Anmeldung ---------------------------------------------------------------------------------


def test_login_requires_second_factor(client, secured):
    user, secret, _codes = secured
    resp = _post_login(client, user)
    assert resp.status_code == 302 and resp.headers["Location"].endswith("/auth/2fa")
    # Nach dem Passwort ist man NICHT angemeldet - kein Bereich erreichbar.
    for path in ("/zeiterfassung", "/leipziger-liste", "/settings/profile", "/settings/sicherheit"):
        resp = client.get(path)
        assert resp.status_code == 302 and "/auth/login" in resp.headers["Location"], path

    assert client.post("/auth/2fa", data={"code": "123456"}).status_code == 200
    assert client.get("/zeiterfassung").status_code == 302

    resp = client.post("/auth/2fa", data={"code": totp_code(user, secret)})
    assert resp.status_code == 302
    assert client.get("/zeiterfassung").status_code == 200


def test_two_factor_step_without_password_is_impossible(client, secured):
    user, secret, _ = secured
    resp = client.post("/auth/2fa", data={"code": totp_code(user, secret)})
    assert resp.status_code == 302 and resp.headers["Location"].endswith("/auth/login")
    assert client.get("/zeiterfassung").status_code == 302


def test_forged_pending_session_for_other_user_fails(client, secured, db, tenant):
    """Wer das Passwort von Konto A kennt, kann den 2FA-Schritt nicht fuer Konto B nutzen."""
    user, secret, _ = secured
    other = User(tenant_id=tenant.id, email="b@example.com", role=UserRole.OFFICE_ADMIN)
    other.set_password("anderes-passwort1")
    db.session.add(other)
    db.session.commit()
    _post_login(client, user)
    with client.session_transaction() as sess:
        sess["_pending_2fa"] = {**sess["_pending_2fa"], "uid": other.id}
    # Konto B hat keine 2FA bzw. eine andere auth_version -> Abbruch statt Anmeldung.
    resp = client.post("/auth/2fa", data={"code": totp_code(user, secret)})
    assert resp.status_code == 302 and resp.headers["Location"].endswith("/auth/login")
    assert client.get("/zeiterfassung").status_code == 302


def test_totp_code_cannot_be_replayed(client, app, secured):
    user, secret, _ = secured
    _post_login(client, user)
    code = totp_code(user, secret)
    assert client.post("/auth/2fa", data={"code": code}).status_code == 302
    client.post("/auth/logout")

    second = app.test_client()
    _post_login(second, user)
    assert second.post("/auth/2fa", data={"code": code}).status_code == 200
    assert second.get("/zeiterfassung").status_code == 302


def test_pending_two_factor_expires(client, app, secured):
    user, secret, _ = secured
    _post_login(client, user)
    with client.session_transaction() as sess:
        expired = int(time.time()) - app.config["TWO_FACTOR_PENDING_MAX_AGE_SECONDS"] - 1
        sess["_pending_2fa"] = {**sess["_pending_2fa"], "ts": expired}
    resp = client.post("/auth/2fa", data={"code": totp_code(user, secret)})
    assert resp.status_code == 302 and resp.headers["Location"].endswith("/auth/login")
    assert client.get("/zeiterfassung").status_code == 302


def test_brute_force_lockout_blocks_even_correct_code(client, app, secured):
    user, secret, _ = secured
    app.config["TWO_FACTOR_MAX_FAILURES"] = 3
    _post_login(client, user)
    for _ in range(3):
        client.post("/auth/2fa", data={"code": "000000"})
    resp = client.post("/auth/2fa", data={"code": totp_code(user, secret)})
    assert resp.status_code == 429
    assert client.get("/zeiterfassung").status_code == 302
    assert AuditLog.query.filter_by(event_type=AuditEventType.TWO_FACTOR_FAILED).count() == 3


def test_recovery_code_works_exactly_once(client, app, secured):
    user, _secret, codes = secured
    _post_login(client, user)
    assert client.post("/auth/2fa", data={"code": codes[0].upper()}).status_code == 302
    assert client.get("/zeiterfassung").status_code == 200
    assert AuditLog.query.filter_by(event_type=AuditEventType.RECOVERY_CODE_USED).count() == 1

    again = app.test_client()
    _post_login(again, user)
    assert again.post("/auth/2fa", data={"code": codes[0]}).status_code == 200
    assert again.get("/zeiterfassung").status_code == 302
    # Ein anderer Code funktioniert weiterhin.
    assert again.post("/auth/2fa", data={"code": codes[1]}).status_code == 302
    assert two_factor.remaining_recovery_codes(user) == 8


def test_regenerating_recovery_codes_invalidates_old_ones(app, auth_client, secured):
    user, secret, codes = secured
    resp = auth_client.post("/settings/sicherheit", data={"code": totp_code(user, secret)})
    assert "Codes gesichert" in resp.get_data(as_text=True)
    client = app.test_client()
    _post_login(client, user)
    assert client.post("/auth/2fa", data={"code": codes[0]}).status_code == 200


def test_recovery_codes_are_bound_to_their_user(app, db, tenant, secured):
    _user, _secret, codes = secured
    other = User(tenant_id=tenant.id, email="b@example.com", role=UserRole.EMPLOYEE)
    other.set_password("anderes-passwort1")
    db.session.add(other)
    db.session.commit()
    _other_secret, _ = enable_two_factor(other)
    client = app.test_client()
    _post_login(client, other, "anderes-passwort1")
    assert client.post("/auth/2fa", data={"code": codes[2]}).status_code == 200
    assert client.get("/zeiterfassung").status_code == 302


# --- Pflicht-Einrichtung ---------------------------------------------------------------------


def test_enforced_two_factor_blocks_everything_until_setup(app, auth_client, user):
    app.config["TWO_FACTOR_ENFORCED"] = True
    for path in ("/leipziger-liste", "/zeiterfassung", "/settings/users", "/settings/profile"):
        resp = auth_client.get(path)
        assert resp.status_code == 302 and resp.headers["Location"].endswith("/settings/sicherheit"), path
    assert auth_client.post("/zeiterfassung/einstempeln").status_code == 403
    assert auth_client.get("/settings/sicherheit").status_code == 200

    secret = two_factor._decrypt(_reload(user).totp_secret_encrypted)
    auth_client.post("/settings/sicherheit", data={"code": pyotp.TOTP(secret).now()})
    assert auth_client.get("/zeiterfassung").status_code == 200


def test_two_factor_reset_by_office_admin(auth_client, db, tenant, employee):
    secret, _ = enable_two_factor(employee)
    resp = auth_client.post(f"/settings/users/{employee.id}/2fa-zuruecksetzen")
    assert resp.status_code == 302
    employee = _reload(employee)
    assert employee.two_factor_enabled is False and employee.totp_secret_encrypted is None
    assert RecoveryCode.query.filter_by(user_id=employee.id).count() == 0
    assert AuditLog.query.filter_by(event_type=AuditEventType.TWO_FACTOR_RESET).count() == 1


def test_office_admin_cannot_reset_two_factor_of_other_tenant(auth_client, db):
    other_tenant = Tenant(name="Fremd", slug="fremd-2fa")
    db.session.add(other_tenant)
    db.session.commit()
    with use_tenant_id(other_tenant.id):
        stranger = User(tenant_id=other_tenant.id, email="fremd@example.com", role=UserRole.EMPLOYEE)
        stranger.set_password("fremdpasswort1")
        db.session.add(stranger)
        db.session.commit()
        enable_two_factor(stranger)
    assert auth_client.post(f"/settings/users/{stranger.id}/2fa-zuruecksetzen").status_code == 404
    assert _reload(stranger).two_factor_enabled is True


def test_employee_cannot_reset_two_factor(employee_client, user):
    enable_two_factor(user)
    assert employee_client.post(f"/settings/users/{user.id}/2fa-zuruecksetzen").status_code == 403
    assert _reload(user).two_factor_enabled is True


# --- Passwort-Reset mit 2FA ------------------------------------------------------------------


@pytest.fixture()
def mail_enabled(app, monkeypatch):
    app.config["SMTP_HOST"] = "smtp.example.test"
    app.config["MAIL_FROM"] = "noreply@example.test"
    mails = []
    monkeypatch.setattr("app.tasks.auth_tasks.send_email", lambda to, subject, body: mails.append((to, subject, body)))
    return mails


def _reset(client, token, code, password="ganz-neues-passwort"):
    return client.post(
        "/auth/reset-password",
        data={"token": token, "code": code, "password": password, "password_confirm": password},
    )


def test_reset_requires_valid_second_factor(client, mail_enabled, secured):
    user, secret, _ = secured
    token = generate_reset_token(user)
    resp = _reset(client, token, "000000")
    assert resp.status_code == 200
    assert _reload(user).check_password(PASSWORD)

    resp = _reset(client, token, totp_code(user, secret))
    assert resp.status_code == 302 and resp.headers["Location"].endswith("/auth/login")
    assert _reload(user).check_password("ganz-neues-passwort")


def test_reset_without_two_factor_is_refused(client, mail_enabled, user):
    resp = _reset(client, generate_reset_token(user), "123456")
    assert resp.status_code == 302 and resp.headers["Location"].endswith("/auth/login")
    assert _reload(user).check_password(PASSWORD)


def test_reset_token_is_single_use(client, mail_enabled, secured):
    user, secret, _ = secured
    token = generate_reset_token(user)
    assert _reset(client, token, totp_code(user, secret)).status_code == 302
    reuse = _reset(client, token, totp_code(user, secret), "noch-ein-passwort1")
    assert reuse.headers["Location"].endswith("/auth/forgot-password")
    assert _reload(user).check_password("ganz-neues-passwort")


def test_reset_token_expires(app, client, mail_enabled, secured, monkeypatch):
    user, secret, _ = secured
    token = generate_reset_token(user)
    app.config["PASSWORD_RESET_TOKEN_MAX_AGE_SECONDS"] = 60
    monkeypatch.setattr("itsdangerous.timed.TimestampSigner.get_timestamp", lambda self: int(time.time()) + 3600)
    resp = _reset(client, token, totp_code(user, secret))
    assert resp.headers["Location"].endswith("/auth/forgot-password")
    assert _reload(user).check_password(PASSWORD)


def test_reset_token_only_changes_its_own_user(app, client, db, tenant, mail_enabled, secured):
    """Das Token gehoert genau einem Konto: der Code eines anderen Kontos hilft nicht, und
    das Token aendert nie ein anderes Konto."""
    user, secret, _ = secured
    other = User(tenant_id=tenant.id, email="b@example.com", role=UserRole.EMPLOYEE)
    other.set_password("anderes-passwort1")
    db.session.add(other)
    db.session.commit()
    other_secret, _ = enable_two_factor(other)

    token = generate_reset_token(user)
    # Code von Konto B fuer das Token von Konto A -> abgelehnt.
    assert _reset(client, token, totp_code(other, other_secret)).status_code == 200
    assert _reload(user).check_password(PASSWORD)

    assert _reset(client, token, totp_code(user, secret)).status_code == 302
    assert _reload(user).check_password("ganz-neues-passwort")
    assert _reload(other).check_password("anderes-passwort1")


def test_reset_ends_existing_sessions(app, client, mail_enabled, secured):
    user, secret, _ = secured
    logged_in = app.test_client()
    _post_login(logged_in, user)
    logged_in.post("/auth/2fa", data={"code": totp_code(user, secret)})
    assert logged_in.get("/zeiterfassung").status_code == 200

    assert _reset(client, generate_reset_token(user), totp_code(user, secret)).status_code == 302
    assert logged_in.get("/zeiterfassung").status_code == 302


def test_reset_with_recovery_code(client, mail_enabled, secured):
    user, _secret, codes = secured
    assert _reset(client, generate_reset_token(user), codes[3]).status_code == 302
    assert _reload(user).check_password("ganz-neues-passwort")


def test_forgot_password_rate_limited_per_ip(app, client, mail_enabled, user):
    app.config["PASSWORD_RESET_MAX_REQUESTS_PER_IP_PER_HOUR"] = 3
    for index in range(3):
        client.post("/auth/forgot-password", data={"email": f"unbekannt{index}@example.com"})
    resp = client.post("/auth/forgot-password", data={"email": user.email})
    assert resp.status_code == 302  # identische Antwort ...
    assert mail_enabled == []  # ... aber kein Versand mehr


def test_login_page_shows_forgot_password_prominently(client, mail_enabled):
    html = client.get("/auth/login").get_data(as_text=True)
    assert "Passwort vergessen?" in html
