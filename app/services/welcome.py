"""Willkommensseite beim ersten Desktop-Besuch der Startseite.

Ein Browser gilt als "bekannt", sobald darin jemand angemeldet war oder die Anmeldeseite
geoeffnet wurde. Dafuer setzen wir das Cookie MARK_COOKIE mit dem festen Wert "1" - ohne
Benutzer-, Buero- oder Geraetebezug. Es steuert ausschliesslich, ob "/" die Willkommensseite
oder direkt die Anmeldung zeigt; nach geloeschten Browserdaten erscheint die Seite erneut.
Handys (und Aufrufe ohne erkennbaren Browser) behalten den bisherigen Einstieg: Anmeldung."""

from flask import current_app, request

MARK_COOKIE = "zentriq_bekannt"
MARK_MAX_AGE = 2 * 365 * 24 * 3600
_MOBILE_TOKENS = ("mobi", "android", "iphone", "ipad", "ipod")


def is_desktop_browser(user_agent: str | None) -> bool:
    ua = (user_agent or "").lower()
    return "mozilla/" in ua and not any(token in ua for token in _MOBILE_TOKENS)


def browser_is_known() -> bool:
    return request.cookies.get(MARK_COOKIE) == "1"


def should_show_welcome() -> bool:
    return not browser_is_known() and is_desktop_browser(request.headers.get("User-Agent"))


def mark_browser(response):
    if not browser_is_known():
        response.set_cookie(
            MARK_COOKIE,
            "1",
            max_age=MARK_MAX_AGE,
            path="/",
            httponly=True,
            samesite="Lax",
            secure=bool(current_app.config.get("SESSION_COOKIE_SECURE", False)),
        )
    return response
