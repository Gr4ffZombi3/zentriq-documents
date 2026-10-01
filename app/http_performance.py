"""Auslieferungs-Optimierungen ohne Aenderung an nginx:

- Browser-Caching: versionierte Assets (?v=..., vendor/ mit Version im Dateinamen) werden ein
  Jahr gecacht, statt bei jedem Seitenwechsel per Gunicorn revalidiert zu werden.
- gzip fuer Text-Antworten (HTML, CSS, JS, JSON), sofern nginx nicht schon komprimiert. Fuer
  statische Dateien wird das Ergebnis pro ETag im Speicher gehalten."""

import gzip

from flask import request

COMPRESSIBLE_MIMETYPES = frozenset(
    {"text/html", "text/css", "text/javascript", "application/javascript", "application/json", "image/svg+xml"}
)
MIN_COMPRESS_BYTES = 1024
LONG_CACHE = "public, max-age=31536000, immutable"
MEDIUM_CACHE = "public, max-age=604800"

_static_gzip_cache: dict[tuple[str, str], bytes] = {}


def _apply_static_cache_headers(response) -> None:
    if request.endpoint != "static" or response.status_code not in (200, 304):
        return
    filename = (request.view_args or {}).get("filename", "")
    if "v" in request.args or filename.startswith("vendor/"):
        response.headers["Cache-Control"] = LONG_CACHE
    elif filename.startswith("fonts/"):
        response.headers["Cache-Control"] = MEDIUM_CACHE


def _compress(response) -> None:
    if (
        response.status_code != 200
        or response.mimetype not in COMPRESSIBLE_MIMETYPES
        or "Content-Encoding" in response.headers
        or (response.is_streamed and not response.direct_passthrough)
        or "gzip" not in request.headers.get("Accept-Encoding", "").lower()
    ):
        return

    is_static = request.endpoint == "static"
    etag, _ = response.get_etag()
    cache_key = (request.path, etag) if is_static and etag else None
    compressed = _static_gzip_cache.get(cache_key) if cache_key else None
    if compressed is None:
        response.direct_passthrough = False
        data = response.get_data()
        if len(data) < MIN_COMPRESS_BYTES:
            return
        compressed = gzip.compress(data, compresslevel=6)
        if cache_key:
            _static_gzip_cache[cache_key] = compressed
    else:
        # Bereits komprimiert: die noch offene Datei der Originalantwort schliessen.
        close = getattr(response.response, "close", None)
        if close is not None:
            close()
        response.direct_passthrough = False

    response.set_data(compressed)
    response.headers["Content-Encoding"] = "gzip"
    response.headers["Content-Length"] = str(len(compressed))
    response.vary.add("Accept-Encoding")
    if etag:
        response.set_etag(f"{etag}-gz")


def register_http_performance(app) -> None:
    @app.before_request
    def _accept_gzip_etag():
        # Komprimierte Antworten tragen "<etag>-gz"; fuer die 304-Pruefung von send_file wird
        # der Suffix wieder entfernt, damit Revalidierungen weiterhin 304 liefern.
        if request.endpoint == "static":
            header = request.environ.get("HTTP_IF_NONE_MATCH")
            if header and "-gz" in header:
                request.environ["HTTP_IF_NONE_MATCH"] = header.replace('-gz"', '"')

    @app.after_request
    def _optimize_response(response):
        _apply_static_cache_headers(response)
        _compress(response)
        return response
