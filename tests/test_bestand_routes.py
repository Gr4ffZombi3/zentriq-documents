from app.models import Customer


def test_bestand_requires_login(client):
    resp = client.get("/bestand")
    assert resp.status_code == 302
    assert "/auth/login" in resp.headers["Location"]


def test_bestand_shows_empty_state_without_data(auth_client, db):
    resp = auth_client.get("/bestand")
    assert resp.status_code == 200
    assert "Dir sind noch keine Kunden zugeordnet." in resp.get_data(as_text=True)


def test_bestand_shows_only_own_customers(auth_client, db, tenant, user):
    from app.models import User

    other_user = User(tenant_id=tenant.id, email="other-broker@example.com")
    other_user.set_password("passwort123")
    db.session.add(other_user)
    db.session.commit()

    own_customer = Customer(tenant_id=tenant.id, name="Eigener Kunde", assigned_user_id=user.id)
    other_customer = Customer(tenant_id=tenant.id, name="Fremder Kunde", assigned_user_id=other_user.id)
    db.session.add_all([own_customer, other_customer])
    db.session.commit()

    resp = auth_client.get("/bestand")
    body = resp.get_data(as_text=True)
    assert "Eigener Kunde" in body
    assert "Fremder Kunde" not in body
