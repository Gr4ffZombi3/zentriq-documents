def test_login_with_correct_credentials_succeeds(client, db, user):
    resp = client.post(
        "/auth/login", data={"login_type": "email", "identifier": user.email, "password": "testpassword123"}
    )
    assert resp.status_code == 302
    assert resp.headers["Location"] == "/"

    dashboard_resp = client.get("/", follow_redirects=True)
    assert dashboard_resp.status_code == 200


def test_login_with_vermittlernummer_succeeds(client, db, user):
    resp = client.post(
        "/auth/login",
        data={"login_type": "vermittlernummer", "identifier": user.vermittlernummer, "password": "testpassword123"},
    )
    assert resp.status_code == 302
    assert resp.headers["Location"] == "/"

    dashboard_resp = client.get("/", follow_redirects=True)
    assert dashboard_resp.status_code == 200


def test_login_with_wrong_password_fails(client, db, user):
    resp = client.post(
        "/auth/login", data={"login_type": "email", "identifier": user.email, "password": "falsches-passwort"}
    )
    assert resp.status_code == 200
    assert "falsch" in resp.get_data(as_text=True)

    protected_resp = client.get("/")
    assert protected_resp.status_code == 302


def test_login_with_unknown_email_fails(client, db):
    resp = client.post(
        "/auth/login", data={"login_type": "email", "identifier": "nobody@example.com", "password": "irrelevant"}
    )
    assert resp.status_code == 200
    assert "falsch" in resp.get_data(as_text=True)


def test_login_with_unknown_vermittlernummer_fails(client, db):
    resp = client.post(
        "/auth/login",
        data={"login_type": "vermittlernummer", "identifier": "VM-9999", "password": "irrelevant"},
    )
    assert resp.status_code == 200
    assert "falsch" in resp.get_data(as_text=True)


def test_protected_route_redirects_to_login_when_unauthenticated(client):
    resp = client.get("/documents")
    assert resp.status_code == 302
    assert "/auth/login" in resp.headers["Location"]


def test_logout_ends_session(auth_client):
    resp = auth_client.get("/", follow_redirects=True)
    assert resp.status_code == 200

    logout_resp = auth_client.post("/auth/logout")
    assert logout_resp.status_code == 302

    after_logout = auth_client.get("/")
    assert after_logout.status_code == 302
    assert "/auth/login" in after_logout.headers["Location"]
