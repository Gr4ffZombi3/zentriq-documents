import pytest

from app.models import AuditLog, EmployeeProfile, Tenant, User, UserRole
from app.models.audit_log import AuditEventType
from app.tenancy import bypass_tenant_scope, use_tenant_id

BASE = {
    "email": "neu@example.com",
    "display_name": "Neue Kollegin",
    "personnel_number": "2001",
    "weekly_hours": "38,5",
    "workdays": ["1", "2", "3", "4", "5"],
    "is_active": "y",
    "password": "startpasswort1",
    "password_confirm": "startpasswort1",
}


def test_admin_creates_employee_in_own_tenant(auth_client, tenant):
    resp = auth_client.post("/settings/users/new", data={**BASE, "role": "mitarbeiter"})
    assert resp.status_code == 302
    created = User.query.filter_by(email="neu@example.com").one()
    assert created.tenant_id == tenant.id
    assert created.role == UserRole.MITARBEITER
    assert created.check_password("startpasswort1")
    assert created.employee_profile.weekly_target_minutes == 2310
    assert created.employee_profile.workdays == "12345"
    assert AuditLog.query.filter_by(event_type=AuditEventType.USER_CREATED).count() == 1


def test_role_must_be_chosen_explicitly(auth_client):
    resp = auth_client.post("/settings/users/new", data=BASE)
    assert resp.status_code == 200
    assert "Bitte eine Rolle auswählen" in resp.get_data(as_text=True)
    assert User.query.filter_by(email="neu@example.com").first() is None


def test_duplicate_email_rejected_even_across_tenants(auth_client, db):
    other = Tenant(name="Fremd", slug="fremd-ua")
    db.session.add(other)
    db.session.commit()
    with use_tenant_id(other.id):
        db.session.add(User(tenant_id=other.id, email="neu@example.com", password_hash="x"))
        db.session.commit()
    resp = auth_client.post("/settings/users/new", data={**BASE, "role": "admin"})
    assert "bereits vergeben" in resp.get_data(as_text=True)
    with bypass_tenant_scope():
        assert User.query.filter_by(email="neu@example.com").count() == 1


def test_admin_updates_and_deactivates_employee(auth_client, employee, db):
    data = {**BASE, "email": employee.email, "role": "mitarbeiter", "weekly_hours": "20", "workdays": ["1", "2"], "password": "", "password_confirm": ""}
    data.pop("is_active")
    resp = auth_client.post(f"/settings/users/{employee.id}", data=data)
    assert resp.status_code == 302
    db.session.refresh(employee)
    assert employee.is_active is False
    assert employee.employee_profile.weekly_target_minutes == 1200
    entry = AuditLog.query.filter_by(event_type=AuditEventType.USER_UPDATED).one()
    assert "is_active" in entry.details["changes"]
    assert "startpasswort1" not in str(entry.details)


def test_admin_cannot_demote_self(auth_client, user, db):
    data = {**BASE, "email": user.email, "role": "mitarbeiter", "password": "", "password_confirm": ""}
    resp = auth_client.post(f"/settings/users/{user.id}", data=data)
    assert "nicht selbst" in resp.get_data(as_text=True)
    db.session.refresh(user)
    assert user.role == UserRole.ADMIN


def test_admin_cannot_edit_foreign_tenant_user(auth_client, db):
    other = Tenant(name="Fremd", slug="fremd-ua2")
    db.session.add(other)
    db.session.commit()
    with use_tenant_id(other.id):
        stranger = User(tenant_id=other.id, email="fremd@example.com", password_hash="x", role=UserRole.ADMIN)
        db.session.add(stranger)
        db.session.commit()
        stranger_id = stranger.id
    assert auth_client.get(f"/settings/users/{stranger_id}").status_code == 404
    assert auth_client.post(f"/settings/users/{stranger_id}", data={**BASE, "role": "mitarbeiter"}).status_code == 404
    assert "fremd@example.com" not in auth_client.get("/settings/users").get_data(as_text=True)


@pytest.mark.parametrize("url", ["/settings/users", "/settings/users/new", "/settings/users/1"])
def test_employee_cannot_manage_users(employee_client, url):
    assert employee_client.get(url).status_code == 403
    assert employee_client.post(url, data={**BASE, "role": "admin"}).status_code in (403, 405)
    with bypass_tenant_scope():
        assert User.query.filter_by(email="neu@example.com").first() is None


def test_settings_index_redirects_by_role(auth_client, employee_client):
    assert auth_client.get("/settings").headers["Location"].endswith("/settings/users")
    assert employee_client.get("/settings").headers["Location"].endswith("/settings/profile")


def test_profile_created_lazily_for_existing_users(auth_client, employee, db):
    assert EmployeeProfile.query.count() == 0
    data = {**BASE, "email": employee.email, "role": "mitarbeiter", "password": "", "password_confirm": ""}
    auth_client.post(f"/settings/users/{employee.id}", data=data)
    assert EmployeeProfile.query.filter_by(user_id=employee.id).count() == 1
