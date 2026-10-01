import re

from app.models import EmployeeProfile


def _greeting(client):
    html = client.get("/uebersicht").get_data(as_text=True)
    return re.search(r'<h1 class="dash-greeting">(.*?)</h1>', html, re.S).group(1).strip()


def test_greeting_without_display_name_shows_no_email_or_username(auth_client):
    greeting = _greeting(auth_client)
    assert greeting in ("Guten Morgen", "Guten Tag", "Guten Abend")


def test_greeting_uses_first_name_from_display_name(auth_client, db, user):
    db.session.add(EmployeeProfile(tenant_id=user.tenant_id, user_id=user.id, display_name="Justin Heller"))
    db.session.commit()
    assert _greeting(auth_client).endswith(", Justin")


def test_greeting_ignores_email_as_display_name(auth_client, db, user):
    db.session.add(EmployeeProfile(tenant_id=user.tenant_id, user_id=user.id, display_name=user.email))
    db.session.commit()
    assert "," not in _greeting(auth_client)


def test_favicon_ico_is_served_without_login(client):
    resp = client.get("/favicon.ico")
    assert resp.status_code == 200
    assert resp.mimetype == "image/x-icon"
    assert resp.data[:4] == b"\x00\x00\x01\x00"


FAVICON_LINK = r'rel="icon" href="/static/img/favicon\.svg\?v=[0-9a-f]{12}"'


def test_login_links_versioned_favicon(client):
    assert re.search(FAVICON_LINK, client.get("/auth/login").get_data(as_text=True))


def test_portal_links_versioned_favicon(auth_client):
    assert re.search(FAVICON_LINK, auth_client.get("/uebersicht").get_data(as_text=True))


def test_login_page_wordmark_toggle_and_forgot_link(client):
    html = client.get("/auth/login").get_data(as_text=True)
    assert 'class="wordmark' in html and "app-brand-mark" not in html
    assert "Versicherungs- und Kfz-Teams" not in html
    assert "Vermittlernummer" in html and "E-Mail" in html
    assert "Passwort vergessen?" in html
