"""Oeffentliche Buchungsseite fuer persoenliche Buchungslinks - ohne Anmeldung, ohne KI.

Der Link berechtigt ausschliesslich zu: freie Zeiten dieser Freigabe sehen, einen Termin buchen.
Die Seite bekommt nur public_view() und die freien Uhrzeiten (keine IDs, keine Kalender-/Kunden-
daten). Antworten werden nicht zwischengespeichert, nicht indexiert, ohne Referrer.

Adresse: /termin/buchen/<token>. Fruehere Links /termin/<token> bleiben gueltig und leiten weiter.
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
    response.headers["Referrer-Policy"] = "no-referrer"
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
    """Bereits verschickte Links (/termin/<token>) bis zu ihrem Ablauf weiter nutzbar."""
    if request.method == "POST" or bl.find(token) is None:
        return _handle(token)
    response = redirect(url_for("public_booking.page", token=token), 301)
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
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
