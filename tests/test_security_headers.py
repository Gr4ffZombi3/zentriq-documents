"""Sicherheitsheader auf allen Antworten (HSTS nur ueber HTTPS)."""


def test_security_headers_on_login_page(client):
    resp = client.get("/auth/login")
    assert resp.headers["X-Content-Type-Options"] == "nosniff"
    assert resp.headers["X-Frame-Options"] == "SAMEORIGIN"
    assert resp.headers["Referrer-Policy"] == "strict-origin-when-cross-origin"
    assert "microphone=(self)" in resp.headers["Permissions-Policy"]
    assert "Strict-Transport-Security" not in resp.headers  # Testclient spricht HTTP


def test_hsts_over_https(client):
    resp = client.get("/auth/login", base_url="https://zentriq.test")
    assert resp.headers["Strict-Transport-Security"] == "max-age=31536000"
