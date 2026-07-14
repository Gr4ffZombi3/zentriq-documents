from app.models import AuditEventType, AuditLog


def test_successful_login_writes_audit_event(client, db, user, tenant):
    client.post("/auth/login", data={"login_type": "email", "identifier": user.email, "password": "testpassword123"})

    entry = AuditLog.query.filter_by(event_type=AuditEventType.LOGIN_SUCCESS).one()
    assert entry.actor_user_id == user.id
    assert entry.actor_email_snapshot == user.email
    assert entry.tenant_id == tenant.id


def test_failed_login_wrong_password_writes_audit_event_without_user(client, db, user):
    client.post(
        "/auth/login", data={"login_type": "email", "identifier": user.email, "password": "falsches-passwort"}
    )

    entry = AuditLog.query.filter_by(event_type=AuditEventType.LOGIN_FAILED).one()
    assert entry.actor_user_id is None
    assert entry.actor_email_snapshot == user.email


def test_failed_login_unknown_email_has_no_tenant(client, db):
    client.post(
        "/auth/login", data={"login_type": "email", "identifier": "unbekannt@example.com", "password": "irrelevant"}
    )

    entry = AuditLog.query.filter_by(event_type=AuditEventType.LOGIN_FAILED).one()
    assert entry.tenant_id is None
    assert entry.actor_email_snapshot == "unbekannt@example.com"


def test_logout_writes_audit_event(auth_client, user, tenant):
    auth_client.post("/auth/logout")

    entry = AuditLog.query.filter_by(event_type=AuditEventType.LOGOUT).one()
    assert entry.actor_user_id == user.id
    assert entry.tenant_id == tenant.id


def test_audit_log_captures_ip_and_user_agent(client, db, user):
    client.post(
        "/auth/login",
        data={"login_type": "email", "identifier": user.email, "password": "testpassword123"},
        headers={"User-Agent": "pytest-agent/1.0"},
    )

    entry = AuditLog.query.filter_by(event_type=AuditEventType.LOGIN_SUCCESS).one()
    assert entry.user_agent == "pytest-agent/1.0"
    assert entry.ip_address is not None
