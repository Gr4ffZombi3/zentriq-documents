"""Willkommensseite beim ersten Desktop-Besuch, Browser-Markierung und Anfrageformular."""

from app.models import AccessRequest
from app.services.welcome import MARK_COOKIE
from tests.test_global_search import _login, _make_user, world  # noqa: F401

DESKTOP = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/130.0 Safari/537.36"}
PHONE = {"User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 Mobile/15E148"}


def _mark(client):
    return client.get_cookie(MARK_COOKIE)


def test_first_desktop_visit_shows_welcome_without_marking(client):
    resp = client.get("/", headers=DESKTOP)
    page = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert "Herzlich willkommen bei ZentriqAI" in page
    for href in ("/auth/login", "/auth/anfrage/buero", "/auth/anfrage/demo"):
        assert f'href="{href}"' in page
    assert _mark(client) is None
    assert client.get("/", headers=DESKTOP).status_code == 200  # weiterhin Willkommensseite


def test_opening_login_marks_browser_and_start_goes_to_login(client):
    client.get("/auth/login", headers=DESKTOP)
    assert _mark(client).value == "1"
    resp = client.get("/", headers=DESKTOP)
    assert resp.status_code == 302 and "/auth/login" in resp.headers["Location"]


def test_phone_and_unknown_clients_keep_login_entry(client):
    for headers in (PHONE, {}):
        resp = client.get("/", headers=headers)
        assert resp.status_code == 302 and "/auth/login" in resp.headers["Location"]


def test_logged_in_user_goes_to_app_and_gets_marked(auth_client):
    resp = auth_client.get("/", headers=DESKTOP)
    assert resp.status_code == 302 and "/auth/login" not in resp.headers["Location"]
    assert _mark(auth_client).value == "1"


def test_direct_links_work_without_detour(client):
    assert client.get("/auth/login", headers=DESKTOP).status_code == 200
    assert client.get("/auth/forgot-password", headers=DESKTOP).status_code == 200


def test_access_request_is_stored_and_confirmed(client, db):
    resp = client.post(
        "/auth/anfrage/demo",
        data={"office_name": "Testbüro", "contact_name": "Erika Muster", "email": "Erika@Example.com", "consent": "y"},
    )
    assert resp.status_code == 302 and "gesendet=1" in resp.headers["Location"]
    item = AccessRequest.query.one()
    assert (item.kind, item.email) == ("demo", "erika@example.com")
    assert "Vielen Dank" in client.get(resp.headers["Location"]).get_data(as_text=True)


def test_access_request_requires_consent_and_ignores_honeypot(client, db):
    data = {"office_name": "Testbüro", "contact_name": "Erika Muster", "email": "erika@example.com"}
    assert client.post("/auth/anfrage/buero", data=data).status_code == 200
    assert client.post("/auth/anfrage/buero", data={**data, "consent": "y", "website": "x"}).status_code == 302
    assert AccessRequest.query.count() == 0
    assert client.get("/auth/anfrage/unbekannt").status_code == 404


def test_access_request_rate_limited(client, db):
    data = {"office_name": "B", "contact_name": "C", "email": "c@example.com", "consent": "y"}
    for _ in range(5):
        assert client.post("/auth/anfrage/buero", data=data).status_code == 302
    assert client.post("/auth/anfrage/buero", data=data).status_code == 429


def test_only_super_admin_sees_requests(app, db, world):  # noqa: F811
    db.session.add(AccessRequest(kind="buero", office_name="Neues Büro", contact_name="X", email="x@example.com"))
    db.session.commit()
    assert "Neues Büro" in _login(app, "plattform@example.com").get("/plattform/anfragen").get_data(as_text=True)
    assert _login(app, "admin-a@example.com").get("/plattform/anfragen").status_code == 403


def test_super_admin_can_delete_request(app, db, world):  # noqa: F811
    item = AccessRequest(kind="demo", office_name="Testbüro", contact_name="X", email="x@example.com")
    db.session.add(item)
    db.session.commit()
    item_id = item.id
    url = f"/plattform/anfragen/{item_id}/loeschen"
    assert _login(app, "admin-a@example.com").post(url).status_code == 403
    assert db.session.get(AccessRequest, item_id) is not None
    resp = _login(app, "plattform@example.com").post(url)
    assert resp.status_code == 302 and resp.headers["Location"].endswith("/plattform/anfragen")
    db.session.expire_all()
    assert db.session.get(AccessRequest, item_id) is None
    assert _login(app, "plattform@example.com").post(url).status_code == 404
