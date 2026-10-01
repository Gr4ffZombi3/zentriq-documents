"""Installierbare Web-App (PWA): Manifest, Service Worker und Offline-Seite.

Der Service Worker speichert ausschliesslich statische Dateien (/static/: CSS, JS, Schrift,
Symbole) und die inhaltsfreie Offline-Seite. Seiten und Daten (Kunden, Leipziger Liste, Memo,
Zeiterfassung) werden nie zwischengespeichert, sondern immer direkt vom Server geladen.
Alle drei Endpunkte sind ohne Anmeldung erreichbar und enthalten keine Benutzerdaten."""

from flask import Blueprint, current_app, jsonify, make_response, render_template, url_for

from app.template_filters import make_static_rev

pwa_bp = Blueprint("pwa", __name__)

THEME_COLOR = "#ffffff"
BACKGROUND_COLOR = "#eef1f5"


def _static_rev():
    return current_app.jinja_env.globals.get("static_rev") or make_static_rev(current_app.static_folder)


@pwa_bp.get("/manifest.webmanifest")
def manifest():
    rev = _static_rev()

    def icon(filename, size, purpose):
        return {
            "src": url_for("static", filename=f"img/{filename}", v=rev(f"img/{filename}")),
            "sizes": f"{size}x{size}",
            "type": "image/png",
            "purpose": purpose,
        }

    response = jsonify(
        {
            "id": "/",
            "name": "Zentriq",
            "short_name": "Zentriq",
            "description": "Zentriq – Leipziger Liste, Memo und Zeiterfassung für Ihr Büro.",
            "lang": "de",
            "start_url": "/",
            "scope": "/",
            "display": "standalone",
            "orientation": "any",
            "theme_color": THEME_COLOR,
            "background_color": BACKGROUND_COLOR,
            "icons": [
                icon("icon-192.png", 192, "any"),
                icon("icon-512.png", 512, "any"),
                icon("icon-maskable-512.png", 512, "maskable"),
            ],
        }
    )
    response.mimetype = "application/manifest+json"
    response.cache_control.public = True
    response.cache_control.max_age = 3600
    return response


@pwa_bp.get("/sw.js")
def service_worker():
    rev = _static_rev()
    # Neue Version bei jeder Aenderung an CSS/JS: alte Caches werden beim Aktivieren entfernt.
    version = "-".join(rev(name) for name in ("css/app.css", "js/app.js"))
    response = make_response(render_template("pwa/sw.js", version=version))
    response.mimetype = "text/javascript"
    response.headers["Service-Worker-Allowed"] = "/"
    response.cache_control.no_cache = True
    return response


@pwa_bp.get("/offline")
def offline():
    return render_template("pwa/offline.html")
