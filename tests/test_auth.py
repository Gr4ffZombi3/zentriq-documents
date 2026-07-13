from app.models import Tenant, User


def test_register_creates_tenant_and_user_and_logs_in(client, db):
    resp = client.post(
        "/auth/register",
        data={
            "company_name": "Neue Firma GmbH",
            "email": "neu@example.com",
            "password": "sicheres-passwort",
            "password_confirm": "sicheres-passwort",
        },
    )
    assert resp.status_code == 302

    tenant = Tenant.query.filter_by(slug="neue-firma-gmbh").first()
    assert tenant is not None

    from app.tenancy import bypass_tenant_scope

    with bypass_tenant_scope():
        user = User.query.filter_by(email="neu@example.com").first()
    assert user is not None
    assert user.tenant_id == tenant.id
    assert user.check_password("sicheres-passwort")

    # Nach Registrierung sofort eingeloggt -> geschuetzte Route erreichbar.
    dashboard_resp = client.get("/")
    assert dashboard_resp.status_code == 200


def test_register_rejects_duplicate_email(client, db, user):
    resp = client.post(
        "/auth/register",
        data={
            "company_name": "Andere Firma",
            "email": user.email,
            "password": "sicheres-passwort",
            "password_confirm": "sicheres-passwort",
        },
    )
    assert resp.status_code == 200
    assert "bereits registriert" in resp.get_data(as_text=True)


def test_register_rejects_mismatched_passwords(client, db):
    resp = client.post(
        "/auth/register",
        data={
            "company_name": "Firma",
            "email": "mismatch@example.com",
            "password": "sicheres-passwort",
            "password_confirm": "anderes-passwort",
        },
    )
    assert resp.status_code == 200
    assert User.query.count() == 0


def test_login_with_correct_credentials_succeeds(client, db, user):
    resp = client.post("/auth/login", data={"email": user.email, "password": "testpassword123"})
    assert resp.status_code == 302
    assert resp.headers["Location"] == "/"

    dashboard_resp = client.get("/")
    assert dashboard_resp.status_code == 200


def test_login_with_wrong_password_fails(client, db, user):
    resp = client.post("/auth/login", data={"email": user.email, "password": "falsches-passwort"})
    assert resp.status_code == 200
    assert "falsch" in resp.get_data(as_text=True)

    protected_resp = client.get("/")
    assert protected_resp.status_code == 302


def test_login_with_unknown_email_fails(client, db):
    resp = client.post("/auth/login", data={"email": "nobody@example.com", "password": "irrelevant"})
    assert resp.status_code == 200
    assert "falsch" in resp.get_data(as_text=True)


def test_protected_route_redirects_to_login_when_unauthenticated(client):
    resp = client.get("/documents")
    assert resp.status_code == 302
    assert "/auth/login" in resp.headers["Location"]


def test_logout_ends_session(auth_client):
    resp = auth_client.get("/")
    assert resp.status_code == 200

    logout_resp = auth_client.post("/auth/logout")
    assert logout_resp.status_code == 302

    after_logout = auth_client.get("/")
    assert after_logout.status_code == 302
    assert "/auth/login" in after_logout.headers["Location"]
