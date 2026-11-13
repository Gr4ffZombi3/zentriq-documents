"""Oeffentliche Buchungsseite fuer persoenliche Buchungslinks - ohne Anmeldung, ohne KI.

Der Link berechtigt ausschliesslich zu: freie Zeiten dieser Freigabe sehen, einen Termin buchen.
Die Seite bekommt nur public_view() und die freien Uhrzeiten (keine IDs, keine Kalender-/Kunden-
daten). Antworten werden nicht zwischengespeichert, nicht indexiert, ohne Referrer.

Adresse: /termin/buchen/<token>. Fruehere Links /termin/<token> bleiben gueltig und leiten weiter.
Allgemeiner Buchungslink je Buero: /termin/<adresse> (app/services/office_booking.py) - bleibt nach
einer Buchung gueltig; Adressen sind hoechstens 40 Zeichen, Tokens 43 - keine Verwechslung.
Nur POST bucht - Oeffnen der Seite oder Abruf der Linkvorschau (WhatsApp & Co.) verbraucht nichts."""

import secrets
from collections import OrderedDict

from flask import Blueprint, make_response, redirect, render_template, request, session, url_for

from app.services import booking_links as bl
from app.services.timetracking.clock import to_local
from app.tenancy import use_tenant_id

public_booking_bp = Blueprint("public_booking", __name__, url_prefix="/termin")
WEEKDAYS = ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"]


def _page(template: str, status: int = 200, **ctx):
    response = make_response(render_template(template, **ctx), status)
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Robots-Tag"] = "noindex, nofollow"
    response.headers["Referrer-Policy"] = "same-origin"  # Flask-WTF prueft bei HTTPS den Referer
    return response


def _notice(title: str, text: str, status: int = 200, view: dict | None = None):
    return _page("public_booking/notice.html", status, title=title, text=text, view=view)


def _ok_key(link) -> str:
    return "bl_ok_" + link.token_hash[:16]


def _label(start_utc) -> dict:
    local = to_local(start_utc)
    return {"value": local.strftime("%Y-%m-%dT%H:%M"), "time": local.strftime("%H:%M"),
            "day": f"{WEEKDAYS[local.weekday()]}, {local.strftime('%d.%m.%Y')}"}


def _grouped(times) -> "OrderedDict[str, list]":
    days: OrderedDict[str, list] = OrderedDict()
    for t in times:
        item = _label(t)
        days.setdefault(item["day"], []).append(item)
    return days


@public_booking_bp.route("/<token>", methods=["GET", "POST"])
def legacy(token):
    """Allgemeiner Buchungslink des Bueros (/termin/<adresse>) bzw. bereits verschickte persoenliche Links
    (/termin/<token>) bis zu ihrem Ablauf."""
    from app.services import office_booking as ob

    page = ob.find(token)
    if page is not None:
        return _office(page)
    if request.method == "POST" or bl.find(token) is None:
        return _handle(token)
    response = redirect(url_for("public_booking.page", token=token), 301)
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "same-origin"  # Flask-WTF prueft bei HTTPS den Referer
    return response


@public_booking_bp.route("/buchen/<token>", methods=["GET", "POST"])
def page(token):
    return _handle(token)


def _handle(token):
    link = bl.find(token)
    if link is None:
        return _notice("Link ungültig", "Dieser Buchungslink ist nicht gültig. Bitte wenden Sie sich an das Büro.", 404)
    with use_tenant_id(link.tenant_id):
        view = bl.public_view(link)
        confirmed = session.get(_ok_key(link))
        if confirmed and (request.method == "GET" or bl.status(link) == "used"):
            return _page("public_booking/confirmed.html", view=view, when=confirmed)
        state = bl.status(link)
        if state == "revoked" or state == "expired":
            return _notice("Link abgelaufen", "Dieser Buchungslink ist abgelaufen oder wurde zurückgezogen. Bitte wenden Sie sich an das Büro.", 410, view)
        if state == "used":
            return _notice("Bereits gebucht", "Über diesen Link wurde bereits ein Termin gebucht. Für Änderungen wenden Sie sich bitte an das Büro.", 200, view)
        error, contact = None, {}
        if request.method == "POST":
            form = request.form
            contact_input = {"name": form.get("name"), "phone": form.get("phone"), "email": form.get("email")}
            try:
                contact = bl.clean_contact(link, **contact_input)
                booked = bl.book(link.id, link.tenant_id, form.get("slot") or "", contact, form.get("submission"))
            except bl.LinkError as exc:
                if exc.code == "gone":
                    return _notice("Link abgelaufen", str(exc), 410, view)
                if exc.code == "used":
                    return _notice("Bereits gebucht", str(exc), 200, view)
                error, contact = str(exc), contact_input
            else:
                local = to_local(booked.start)
                session[_ok_key(link)] = {"day": f"{WEEKDAYS[local.weekday()]}, {local.strftime('%d.%m.%Y')}", "time": local.strftime("%H:%M")}
                return redirect(url_for("public_booking.page", token=token), 303)
        free = bl.free_times(link)
        if not free:
            return _notice("Keine freien Zeiten", "Alle freigegebenen Zeiten sind inzwischen vergeben. Bitte wenden Sie sich an das Büro.", 200, view)
        return _page("public_booking/page.html", 409 if error else 200, view=view, days=_grouped(free), error=error,
                     contact=contact, selected=request.form.get("slot") if error else None, submission=secrets.token_urlsafe(16))


# --- Allgemeiner Buchungslink des Bueros ------------------------------------------------------------


def _office_days(page):
    from app.services import office_booking as ob

    out = []
    for day, times in ob.days(page):
        out.append({"label": f"{WEEKDAYS[day.weekday()]}, {day.strftime('%d.%m.%Y')}", "items": [_label(t) for t in times]})
    return out


def _office(page):
    from app.services import office_booking as ob

    with use_tenant_id(page.tenant_id):
        view = ob.public_view(page)
        key = f"ob_ok_{page.id}"
        if request.method == "GET" and request.args.get("gebucht") == "1" and session.get(key):
            return _page("public_booking/confirmed.html", view=view, when=session.get(key), again_url=url_for("public_booking.legacy", token=page.slug))
        if not ob.usable(page):
            return _notice("Online-Buchung nicht verfügbar", "Über diesen Link können derzeit keine Termine gebucht werden. Bitte wenden Sie sich direkt an das Büro.", 200, view)
        error, contact, selected = None, {}, None
        if request.method == "POST":
            form = request.form
            contact = {"name": form.get("name"), "phone": form.get("phone"), "email": form.get("email")}
            selected = form.get("slot") or ""
            try:
                cleaned = ob.clean_contact(**contact)
                booked = ob.book(page.id, page.tenant_id, selected, cleaned, form.get("submission"))
            except ob.BookingError as exc:
                error = str(exc)
                if exc.code == "taken":
                    selected = None  # Auswahl wurde aktualisiert - die alte Zeit gibt es nicht mehr
            else:
                local = to_local(booked.start)
                session[key] = {"day": f"{WEEKDAYS[local.weekday()]}, {local.strftime('%d.%m.%Y')}", "time": local.strftime("%H:%M")}
                return redirect(url_for("public_booking.legacy", token=page.slug, gebucht="1"), 303)
        days = _office_days(page)
        return _page("public_booking/office.html", 409 if error else 200, view=view, days=days, error=error, contact=contact,
                     selected=selected, submission=secrets.token_urlsafe(16), has_free=any(d["items"] for d in days))
