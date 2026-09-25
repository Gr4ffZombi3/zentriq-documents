"""Serverseitige Rechtepruefung: Mitarbeiter erreichen ausschliesslich die Zeiterfassung."""

import pytest

from app.auth.permissions import is_endpoint_allowed_for_employee
from app.models import UserRole

ADMIN_ONLY_URLS = [
    "/",
    "/mailbox",
    "/mailbox/1",
    "/documents",
    "/documents/1",
    "/documents/live",
    "/potenziale",
    "/potenziale/vergleich",
    "/customers",
    "/customers/1",
    "/bestand",
    "/cockpit",
    "/tasks",
    "/recommendations",
    "/search?q=test",
    "/settings/users",
]


@pytest.mark.parametrize("url", ADMIN_ONLY_URLS)
def test_employee_cannot_open_admin_areas_by_direct_url(employee_client, url):
    resp = employee_client.get(url)
    assert resp.status_code in (302, 403)
    if resp.status_code == 302:
        # Die Startseite leitet Mitarbeiter in die Zeiterfassung, niemals in Admin-Bereiche.
        assert "/zeiterfassung" in resp.headers["Location"]


@pytest.mark.parametrize(
    "url",
    ["/upload", "/mailbox/sync", "/mailbox/1/retry", "/mailbox/1/update", "/documents/1/retry", "/api/chat"],
)
def test_employee_cannot_trigger_admin_actions(employee_client, url):
    assert employee_client.post(url).status_code == 403


def test_employee_can_open_profile(employee_client):
    assert employee_client.get("/settings/profile").status_code == 200


def test_admin_keeps_access_to_existing_areas(auth_client):
    for url in ("/documents", "/potenziale", "/customers", "/mailbox/does-not-exist"):
        assert auth_client.get(url).status_code in (200, 404)


def test_unknown_blueprint_is_denied_by_default():
    assert is_endpoint_allowed_for_employee("documents.list_documents", "documents") is False
    assert is_endpoint_allowed_for_employee("irgendwas.neu", "irgendwas") is False
    assert is_endpoint_allowed_for_employee("auth.logout", "auth") is True


def test_new_users_default_to_least_privilege(db, tenant):
    from app.models import User

    user = User(tenant_id=tenant.id, email="neu@example.com", password_hash="x")
    db.session.add(user)
    db.session.commit()
    assert user.role == UserRole.MITARBEITER
    assert user.is_admin is False


def test_deactivated_user_loses_session_immediately(employee_client, employee, db):
    assert employee_client.get("/settings/profile").status_code == 200
    employee.is_active = False
    db.session.commit()
    resp = employee_client.get("/settings/profile")
    assert resp.status_code == 302
    assert "/auth/login" in resp.headers["Location"]
