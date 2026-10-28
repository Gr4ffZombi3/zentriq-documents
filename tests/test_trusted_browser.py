"""Zwei-Faktor-Authentifizierung: "Diesem Browser 30 Tage vertrauen".

Jeder Test-Client entspricht einem eigenen Browser (eigene Cookies)."""

from datetime import timedelta

import pytest

from app.extensions import db
from app.models import AuditLog, Tenant, TenantStatus, TrustedBrowser, User, UserRole
from app.models.audit_log import AuditEventType
from app.services import trusted_browsers
from app.services.two_factor import reset_two_factor
from app.tenancy import bypass_tenant_scope, use_tenant_id
from tests.two_factor_helpers import enable_two_factor, totp_code

PASSWORD = "testpassword123"


@pytest.fixture()
def secured(user):
    secret, _codes = enable_two_factor(user)
    return user, secret


def _password(client, user, password=PASSWORD):
    return client.post("/auth/login", data={"login_type": "email", "identifier": user.email, "password": password})


def _two_factor(client, user, secret, trust):
    data = {"code": totp_code(user, secret)}
    if trust:
        data["trust_browser"] = "y"
    return client.post("/auth/2fa", data=data)


def _full_login(client, user, secret, trust=False):
    resp = _password(client, user)
    assert resp.headers["Location"].endswith("/auth/2fa")
    resp = _two_factor(client, user, secret, trust)
    assert resp.status_code == 302 and not resp.headers["Location"].endswith("/auth/2fa")
    return resp


def _logged_in(client) -> bool:
    return client.get("/settings/profile").status_code == 200


def _logout(client, forget=False):
    return client.post("/auth/logout", data={"forget_browser": "1"} if forget else {})


def _records(user_id):
    db.session.expire_all()
    return TrustedBrowser.query.filter_by(user_id=user_id).order_by(TrustedBrowser.id).all()


def _reload(user):
    db.session.expire_all()
    with bypass_tenant_scope():
        return db.session.get(User, user.id)


# --- Vertrauter Browser ------------------------------------------------------------------------


def test_trusted_browser_logs_in_with_password_only(app, client, secured):
    user, secret = secured
    resp = _full_login(client, user, secret, trust=True)
    cookie = resp.headers.getlist("Set-Cookie")
    trust_cookie = next(c for c in cookie if c.startswith(trusted_browsers.TRUST_COOKIE + "="))
    assert "HttpOnly" in trust_cookie and "SameSite=Strict" in trust_cookie and "Path=/auth" in trust_cookie
    assert "Max-Age=2592000" in trust_cookie  # 30 Tage
    # Nur der Hash steht in der Datenbank, kein Geheimnis im Cookie.
    token = trust_cookie.split(";")[0].split("=", 1)[1]
    record = _records(user.id)[0]
    assert record.token_hash != token and len(record.token_hash) == 64
    assert secret not in trust_cookie

    _logout(client)
    assert not _logged_in(client)
    resp = _password(client, user)
    assert resp.status_code == 302 and not resp.headers["Location"].endswith("/auth/2fa")
    assert _logged_in(client)
    entry = AuditLog.query.filter_by(event_type=AuditEventType.LOGIN_SUCCESS).order_by(AuditLog.id.desc()).first()
    assert entry.details == {"two_factor": "trusted_browser"}
    # Passwort bleibt Pflicht.
    _logout(client)
    assert _password(client, user, "falsch").status_code == 200
    assert not _logged_in(client)


def test_trust_cookie_is_secure_in_production_config(app, client, secured):
    user, secret = secured
    app.config["TRUSTED_BROWSER_COOKIE_SECURE"] = True
    https = app.test_client()
    https.post("/auth/login", data={"login_type": "email", "identifier": user.email, "password": PASSWORD}, base_url="https://localhost")
    resp = https.post("/auth/2fa", data={"code": totp_code(user, secret), "trust_browser": "y"}, base_url="https://localhost")
    trust_cookie = next(c for c in resp.headers.getlist("Set-Cookie") if c.startswith(trusted_browsers.TRUST_COOKIE + "="))
    assert "Secure" in trust_cookie and "HttpOnly" in trust_cookie


def test_other_browser_still_needs_two_factor(app, client, secured):
    user, secret = secured
    _full_login(client, user, secret, trust=True)
    other = app.test_client()
    assert _password(other, user).headers["Location"].endswith("/auth/2fa")
    assert not _logged_in(other)


def test_without_option_behaviour_is_unchanged(app, client, secured):
    user, secret = secured
    resp = _full_login(client, user, secret, trust=False)
    assert not any(c.startswith(trusted_browsers.TRUST_COOKIE) for c in resp.headers.getlist("Set-Cookie"))
    assert _records(user.id) == []
    _logout(client)
    assert _password(client, user).headers["Location"].endswith("/auth/2fa")


def test_trust_belongs_to_one_user_only(app, client, secured, db, tenant):
    user, secret = secured
    other_user = User(tenant_id=tenant.id, email="kollege@example.com", is_active=True, role=UserRole.EMPLOYEE)
    other_user.set_password(PASSWORD)
    db.session.add(other_user)
    db.session.commit()
    other_secret, _ = enable_two_factor(other_user)
    _full_login(client, user, secret, trust=True)
    _logout(client)
    # Kollege am selben Rechner: braucht eigenen Code.
    assert _password(client, other_user).headers["Location"].endswith("/auth/2fa")
    _two_factor(client, other_user, other_secret, trust=True)
    _logout(client)
    # Beide Konten sind jetzt in diesem Browser vertraut (ein Cookie, zwei Tokens).
    for account in (user, other_user):
        resp = _password(client, account)
        assert not resp.headers["Location"].endswith("/auth/2fa")
        _logout(client)


# --- Ablauf und Widerruf ------------------------------------------------------------------------


def test_trust_expires_after_30_days_and_is_only_renewed_by_two_factor(app, client, secured):
    user, secret = secured
    _full_login(client, user, secret, trust=True)
    _logout(client)
    record = _records(user.id)[0]
    record.expires_at = record.expires_at - timedelta(days=30, seconds=1)
    db.session.commit()
    # Abgelaufen: wieder 2FA.
    assert _password(client, user).headers["Location"].endswith("/auth/2fa")
    # Eine Anmeldung ueber den vertrauten Browser verlaengert nichts.
    _two_factor(client, user, secret, trust=True)
    fresh = [r for r in _records(user.id) if r.revoked_at is None and r.expires_at > record.expires_at]
    assert len(fresh) == 1
    expires = fresh[0].expires_at
    _logout(client)
    _password(client, user)
    _logout(client)
    assert _records(user.id)[-1].expires_at == expires


def test_logout_keeps_trust_but_forget_logout_revokes_it(app, client, secured):
    user, secret = secured
    _full_login(client, user, secret, trust=True)
    _logout(client)
    resp = _password(client, user)
    assert not resp.headers["Location"].endswith("/auth/2fa")
    resp = _logout(client, forget=True)
    assert resp.status_code == 302 and not _logged_in(client)
    assert all(r.revoked_at is not None for r in _records(user.id))
    assert _password(client, user).headers["Location"].endswith("/auth/2fa")


@pytest.mark.parametrize("action", ["password_change", "two_factor_reset", "deactivate", "revoke_all_devices", "tenant_suspended"])
def test_trust_is_revoked_server_side(app, client, secured, tenant, action):
    user, secret = secured
    _full_login(client, user, secret, trust=True)
    other = app.test_client()  # zweiter vertrauter Browser
    _full_login(other, user, secret, trust=True)
    assert len([r for r in _records(user.id) if r.revoked_at is None]) == 2

    if action == "password_change":
        resp = client.post(
            "/settings/profile",
            data={"current_password": PASSWORD, "new_password": "neues-passwort-123", "new_password_confirm": "neues-passwort-123"},
        )
        assert resp.status_code == 302
    elif action == "two_factor_reset":
        with use_tenant_id(tenant.id):
            admin = User(tenant_id=tenant.id, email="chef@example.com", is_active=True, role=UserRole.OFFICE_ADMIN)
            admin.set_password(PASSWORD)
            db.session.add(admin)
            db.session.commit()
            reset_two_factor(_reload(user), admin)
    elif action == "deactivate":
        with use_tenant_id(tenant.id):
            target = _reload(user)
            target.is_active = False
            target.invalidate_sessions()
            db.session.commit()
    elif action == "revoke_all_devices":
        assert client.post("/settings/sitzungen/abmelden").status_code == 302
    elif action == "tenant_suspended":
        root_tenant = Tenant(name="Plattform", slug="plattform")
        db.session.add(root_tenant)
        db.session.commit()
        root = User(tenant_id=root_tenant.id, email="root@example.com", is_active=True, role=UserRole.SUPER_ADMIN)
        root.set_password(PASSWORD)
        db.session.add(root)
        db.session.commit()
        admin_client = app.test_client()
        admin_client.post("/auth/login", data={"login_type": "email", "identifier": root.email, "password": PASSWORD})
        resp = admin_client.post(f"/plattform/bueros/{tenant.id}", data={"name": tenant.name})
        assert resp.status_code == 302
        assert db.session.get(Tenant, tenant.id).status == TenantStatus.SUSPENDED

    assert all(r.revoked_at is not None for r in _records(user.id))
    if action in ("password_change", "revoke_all_devices"):
        # Der zweite Browser schickt sein (jetzt widerrufenes) Token weiter mit: trotzdem 2FA.
        password = "neues-passwort-123" if action == "password_change" else PASSWORD
        resp = other.post("/auth/login", data={"login_type": "email", "identifier": user.email, "password": password})
        assert resp.headers["Location"].endswith("/auth/2fa")


def test_password_reset_revokes_trust(app, client, secured):
    from app.services.password_reset import generate_reset_token

    user, secret = secured
    _full_login(client, user, secret, trust=True)
    _logout(client)
    app.config.update(SMTP_HOST="smtp.test", MAIL_FROM="noreply@zentriq.test")
    token = generate_reset_token(_reload(user))
    resp = app.test_client().post(
        "/auth/reset-password",
        data={"token": token, "code": totp_code(_reload(user), secret), "password": "ganz-neues-pw-123", "password_confirm": "ganz-neues-pw-123", "confirm_password": "ganz-neues-pw-123"},
    )
    assert resp.status_code == 302
    assert all(r.revoked_at is not None for r in _records(user.id))
    assert client.post(
        "/auth/login", data={"login_type": "email", "identifier": user.email, "password": "ganz-neues-pw-123"}
    ).headers["Location"].endswith("/auth/2fa")


def test_locked_two_factor_is_never_skipped(app, client, secured, monkeypatch):
    from app.services import two_factor

    user, secret = secured
    _full_login(client, user, secret, trust=True)
    _logout(client)
    monkeypatch.setattr(two_factor, "is_locked", lambda _user: True)
    assert _password(client, user).headers["Location"].endswith("/auth/2fa")


def test_manipulated_cookie_is_ignored(app, client, secured):
    user, secret = secured
    client.set_cookie(trusted_browsers.TRUST_COOKIE, "erfunden.token", path="/auth")
    assert _password(client, user).headers["Location"].endswith("/auth/2fa")


# --- Sitzung --------------------------------------------------------------------------------


def test_session_is_permanent_with_sliding_lifetime(app, client, secured):
    user, secret = secured
    resp = _full_login(client, user, secret)
    session_cookie = next(c for c in resp.headers.getlist("Set-Cookie") if c.startswith("session="))
    # Ueberlebt einen Browser-Neustart (Expires gesetzt) statt beim Schliessen zu verfallen.
    assert "Expires=" in session_cookie


def test_idle_session_expires_server_side(app, client, secured):
    from app.models import UserSession

    user, secret = secured
    _full_login(client, user, secret)
    assert _logged_in(client)
    record = UserSession.query.filter_by(user_id=user.id).first()
    record.last_seen_at = record.last_seen_at - timedelta(hours=9)
    db.session.commit()
    assert not _logged_in(client)


def test_trusted_login_keeps_roles(app, db, tenant, secured):
    """Eine Anmeldung ueber den vertrauten Browser aendert keine Rechte: Mitarbeiter bleiben
    Mitarbeiter (Rollen-Hook), Buerotrennung bleibt."""
    user, secret = secured
    employee = User(tenant_id=tenant.id, email="ma@example.com", is_active=True, role=UserRole.EMPLOYEE)
    employee.set_password(PASSWORD)
    db.session.add(employee)
    db.session.commit()
    employee_secret, _ = enable_two_factor(employee)
    browser = app.test_client()
    _full_login(browser, employee, employee_secret, trust=True)
    _logout(browser)
    _password(browser, employee)
    assert browser.get("/settings/profile").status_code == 200
    assert browser.get("/customers").status_code == 403
    assert browser.get("/settings/users").status_code == 403
