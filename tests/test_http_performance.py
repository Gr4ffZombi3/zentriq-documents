"""Auslieferung: Browser-Caching fuer versionierte Assets und gzip fuer Text-Antworten."""

import gzip


def test_versioned_static_is_cached_long_and_gzipped(client):
    resp = client.get("/static/css/app.css?v=test", headers={"Accept-Encoding": "gzip"})
    assert resp.status_code == 200
    assert "max-age=31536000" in resp.headers["Cache-Control"]
    assert resp.headers["Content-Encoding"] == "gzip"
    assert gzip.decompress(resp.data).startswith(b"/*! tailwindcss")


def test_gzip_etag_still_revalidates(client):
    first = client.get("/static/css/app.css?v=test", headers={"Accept-Encoding": "gzip"})
    again = client.get(
        "/static/css/app.css?v=test",
        headers={"Accept-Encoding": "gzip", "If-None-Match": first.headers["ETag"]},
    )
    assert again.status_code == 304


def test_vendor_scripts_are_local(client):
    html = client.get("/auth/login").get_data(as_text=True)
    assert "unpkg.com" not in html
    assert "/static/vendor/htmx-2.0.4.min.js" in html
    assert client.get("/static/vendor/htmx-2.0.4.min.js").status_code == 200


def test_html_is_compressed_only_when_accepted(client):
    plain = client.get("/auth/login")
    assert "Content-Encoding" not in plain.headers
    compressed = client.get("/auth/login", headers={"Accept-Encoding": "gzip, br"})
    assert compressed.headers["Content-Encoding"] == "gzip"
    assert gzip.decompress(compressed.data) == plain.data
