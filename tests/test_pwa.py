"""Installierbare Web-App: Manifest, Service Worker, Offline-Seite - ohne Benutzerdaten."""

from app.models import UserRole
from tests.test_global_search import _login, _make_user


def test_manifest_is_valid_and_public(client):
    resp = client.get("/manifest.webmanifest")
    assert resp.status_code == 200
    assert resp.mimetype == "application/manifest+json"
    manifest = resp.get_json()
    assert manifest["name"] == manifest["short_name"] == "Zentriq"
    assert manifest["display"] == "standalone"
    assert manifest["start_url"] == "/" and manifest["scope"] == "/"
    sizes = {(icon["sizes"], icon["purpose"]) for icon in manifest["icons"]}
    assert {("192x192", "any"), ("512x512", "any"), ("512x512", "maskable")} <= sizes
    for icon in manifest["icons"]:
        assert client.get(icon["src"]).status_code == 200


def test_service_worker_caches_only_static_files(client):
    resp = client.get("/sw.js")
    assert resp.status_code == 200
    assert resp.mimetype == "text/javascript"
    assert resp.headers["Service-Worker-Allowed"] == "/"
    assert "no-cache" in resp.headers["Cache-Control"]
    script = resp.get_data(as_text=True)
    # Nur /static/ wird zwischengespeichert; Seitenaufrufe gehen immer an den Server.
    assert 'url.pathname.startsWith("/static/")' in script
    assert 'request.mode === "navigate"' in script
    assert script.count("cache.put(") == 1
    assert 'request.method !== "GET"' in script


def test_offline_page_contains_no_user_data(client):
    resp = client.get("/offline")
    assert resp.status_code == 200
    assert "Keine Verbindung" in resp.get_data(as_text=True)


def test_pages_link_manifest_and_register_service_worker(app, auth_client):
    html = app.test_client().get("/auth/login").get_data(as_text=True)
    assert 'rel="manifest" href="/manifest.webmanifest"' in html
    assert 'name="theme-color"' in html
    assert "serviceWorker" in auth_client.get("/static/js/app.js").get_data(as_text=True)


def test_pwa_endpoints_work_for_every_role_and_during_forced_2fa_setup(app, db, tenant):
    _make_user(db, tenant.id, "plattform@example.org", UserRole.SUPER_ADMIN, None)
    _make_user(db, tenant.id, "ma@example.org", UserRole.EMPLOYEE, "08/1234-A")
    app.config["TWO_FACTOR_ENFORCED"] = True
    for email in ("plattform@example.org", "ma@example.org"):
        client = _login(app, email)
        for url in ("/manifest.webmanifest", "/sw.js", "/offline", "/favicon.ico"):
            assert client.get(url).status_code == 200, (email, url)
