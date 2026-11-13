"""Gemeinsamer Buerokalender (Seite + JSON-API).

Zugriff: Buero-Admins und Mitarbeiter des eigenen Bueros (SUPER_ADMIN nie). Mandantentrennung
ueber den globalen Tenant-Filter; Termine werden per get_or_404_scoped geladen - Termine anderer
Bueros sind fuer jede Aktion 404. Loeschen: Ersteller oder Buero-Admin; absagen/aendern: alle."""

import re
from datetime import date, timedelta

from flask import Blueprint, abort, current_app, flash, jsonify, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from app.extensions import db
from app.models import CalendarEvent
from app.services import calendar as cal
from app.services.global_search import MIN_QUERY_LENGTH, normalize_query, search_customers
from app.services.timetracking.clock import local_today
from app.tenancy import get_or_404_scoped

calendar_bp = Blueprint("calendar", __name__, url_prefix="/kalender")

MAX_RANGE_DAYS = 62


def _office_member():
    if not (current_user.is_office_admin or current_user.is_employee):
        abort(403)


def _error(exc: cal.CalendarError):
    return jsonify({"error": str(exc), "code": exc.code}), exc.status


def _payload() -> dict:
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else {}


def _parse_day(value: str | None, default: date) -> date:
    try:
        return date.fromisoformat(value) if value else default
    except ValueError:
        return default


@calendar_bp.get("")
@login_required
def index():
    _office_member()
    members = cal.member_payload(cal.office_members())
    return render_template(
        "calendar/index.html",
        members=members,
        today=local_today().isoformat(),
        me=current_user.id,
        location_kinds=cal.LOCATION_KINDS,
        prefill={key: request.args.get(key, "") for key in ("titel", "start", "ende", "mitarbeiter", "kunde", "kunde_name", "notiz")},
    )


@calendar_bp.get("/api/termine")
@login_required
def api_events():
    _office_member()
    today = local_today()
    start = _parse_day(request.args.get("von"), today)
    end = _parse_day(request.args.get("bis"), start + timedelta(days=6))
    if end < start or (end - start).days > MAX_RANGE_DAYS:
        return jsonify({"error": "Ungültiger Zeitraum."}), 400
    start_utc, end_utc = cal.local_day_bounds(start, end)
    events = cal.events_between(start_utc, end_utc)
    return jsonify(
        {
            "events": [cal.serialize(item, current_user) for item in events],
            "members": cal.member_payload(cal.office_members()),
            "stamp": cal.change_stamp(),
        }
    )


@calendar_bp.get("/api/stand")
@login_required
def api_stamp():
    _office_member()
    return jsonify({"stamp": cal.change_stamp()})


@calendar_bp.get("/api/naechste")
@login_required
def api_upcoming():
    _office_member()
    mine = request.args.get("nur_meine") == "1"
    items = cal.upcoming(limit=5, assignee_id=current_user.id if mine else None)
    return jsonify({"events": [cal.serialize(item, current_user) for item in items]})


@calendar_bp.get("/api/termine/<int:event_id>")
@login_required
def api_event(event_id):
    _office_member()
    event = get_or_404_scoped(CalendarEvent, event_id)
    return jsonify({"event": cal.serialize(event, current_user), "history": cal.history(event.id)})


def _assignee_lock(assignee_id):
    """Gleiche Sperre wie Buchungen ueber Eingabefeld und Kunden-Buchungslink
    (booking.book, booking_links.book): keine gleichzeitige Belegung desselben Mitarbeiters."""
    from app.services.customer_lock import NamedLock

    try:
        key = int(assignee_id)
    except (TypeError, ValueError):
        key = 0
    return NamedLock(f"zentriq-cal-{current_user.tenant_id}-{key}", 15)


@calendar_bp.post("/api/termine")
@login_required
def api_create():
    from app.services.customer_lock import CustomerLockTimeout, fresh_transaction

    _office_member()
    data = _payload()
    lock = _assignee_lock(data.get("assignee_id"))
    try:
        lock.acquire()
    except CustomerLockTimeout:
        return jsonify({"error": "Der Kalender wird gerade geändert. Bitte erneut speichern.", "code": "busy"}), 409
    try:
        fresh_transaction()
        event, created, conflicts = cal.create_event(current_user, data, client_ref=data.get("client_ref"))
    except cal.CalendarError as exc:
        return _error(exc)
    finally:
        lock.release()
    return jsonify(
        {"event": cal.serialize(event, current_user), "created": created, "conflicts": cal.conflict_payload(conflicts)}
    ), (201 if created else 200)


@calendar_bp.patch("/api/termine/<int:event_id>")
@login_required
def api_update(event_id):
    _office_member()
    from app.services.customer_lock import CustomerLockTimeout

    event = get_or_404_scoped(CalendarEvent, event_id)
    data = _payload()
    lock = _assignee_lock(data.get("assignee_id") or event.assignee_user_id)
    try:
        lock.acquire()
    except CustomerLockTimeout:
        return jsonify({"error": "Der Kalender wird gerade geändert. Bitte erneut speichern.", "code": "busy"}), 409
    try:
        event, changed, conflicts = cal.update_event(current_user, event, data)
    except cal.CalendarError as exc:
        if exc.code == "conflict":
            fresh = get_or_404_scoped(CalendarEvent, event_id)
            return jsonify({"error": str(exc), "code": exc.code, "event": cal.serialize(fresh, current_user)}), 409
        return _error(exc)
    finally:
        lock.release()
    return jsonify({"event": cal.serialize(event, current_user), "changed": changed, "conflicts": cal.conflict_payload(conflicts)})


@calendar_bp.post("/api/termine/<int:event_id>/absagen")
@login_required
def api_cancel(event_id):
    _office_member()
    event = get_or_404_scoped(CalendarEvent, event_id)
    try:
        event = cal.set_cancelled(current_user, event, True, _payload().get("version"))
    except cal.CalendarError as exc:
        return _error(exc)
    return jsonify({"event": cal.serialize(event, current_user)})


@calendar_bp.post("/api/termine/<int:event_id>/reaktivieren")
@login_required
def api_reactivate(event_id):
    _office_member()
    event = get_or_404_scoped(CalendarEvent, event_id)
    try:
        event = cal.set_cancelled(current_user, event, False, _payload().get("version"))
    except cal.CalendarError as exc:
        return _error(exc)
    return jsonify({"event": cal.serialize(event, current_user)})


@calendar_bp.delete("/api/termine/<int:event_id>")
@login_required
def api_delete(event_id):
    _office_member()
    event = get_or_404_scoped(CalendarEvent, event_id)
    try:
        cal.delete_event(current_user, event, _payload().get("version"))
    except cal.CalendarError as exc:
        return _error(exc)
    return jsonify({"deleted": True})


@calendar_bp.get("/api/konflikte")
@login_required
def api_conflicts():
    _office_member()
    try:
        starts_at = cal.parse_local(request.args.get("start"), "den Beginn")
        ends_at = cal.parse_local(request.args.get("ende"), "das Ende")
        assignee_id = int(request.args.get("mitarbeiter", "0"))
    except (cal.CalendarError, ValueError):
        return jsonify({"conflicts": []})
    exclude = request.args.get("ohne", type=int)
    if ends_at <= starts_at:
        return jsonify({"conflicts": []})
    return jsonify({"conflicts": cal.conflict_payload(cal.conflicts_for(assignee_id, starts_at, ends_at, exclude_id=exclude))})


@calendar_bp.get("/api/kunden")
@login_required
def api_customers():
    """Kundenauswahl fuer die Terminverknuepfung - dieselbe Suche wie die Kopfzeilensuche
    (Mitarbeiter finden Kunden des eigenen Bueros, Tenant-Filter)."""
    _office_member()
    query = normalize_query(request.args.get("q"))
    if len(query) < MIN_QUERY_LENGTH:
        return jsonify({"customers": []})
    rows, _more = search_customers(query)
    return jsonify(
        {
            "customers": [
                {"id": row["id"], "name": row["name"], "detail": " · ".join(str(v) for v in (row.get("customer_number"), row.get("city")) if v)}
                for row in rows[:8]
            ]
        }
    )


# --- Einstellungen: Buero-Profil und Terminplanung (Buero-Admins) -------------------------------

_DAY_LABELS = ("Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag")


@calendar_bp.route("/einstellungen", methods=["GET", "POST"])
@login_required
def settings():
    from app.extensions import db
    from app.models import OfficeSettings
    from app.services import availability

    if not current_user.is_office_admin:
        abort(403)
    record = OfficeSettings.query.first()
    errors = []
    if request.method == "POST":
        if record is None:
            record = OfficeSettings(tenant_id=current_user.tenant_id)
            db.session.add(record)
        form = request.form

        def text(key, limit):
            value = " ".join((form.get(key) or "").split())
            return value[:limit] or None

        record.office_name = text("office_name", 200)
        record.email = text("email", 255)
        if record.email and not re.fullmatch(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+", record.email):
            errors.append("Die E-Mail-Adresse des Büros ist ungültig.")
        record.phone = text("phone", 50)
        record.address = text("address", 255)
        record.website = text("website", 255)
        record.signature = (form.get("signature") or "").strip()[:2000] or None
        hours = {}
        for iso in range(1, 8):
            start, end = (form.get(f"von_{iso}") or "").strip(), (form.get(f"bis_{iso}") or "").strip()
            if not start and not end:
                continue
            if not (availability._parse_hm(start) and availability._parse_hm(end) and availability._parse_hm(start) < availability._parse_hm(end)):
                errors.append(f"{_DAY_LABELS[iso - 1]}: bitte Beginn und Ende als HH:MM angeben (Beginn vor Ende).")
                continue
            hours[str(iso)] = [start.zfill(5), end.zfill(5)]
        record.hours = hours or None
        for key, low, high in (("default_minutes", 10, 240), ("buffer_minutes", 0, 120), ("lead_minutes", 0, 7 * 24 * 60), ("horizon_days", 1, 60)):
            try:
                value = int(form.get(key) or 0)
            except ValueError:
                value = -1
            if not low <= value <= high:
                errors.append(f"Ungültiger Wert für {key}.")
            else:
                setattr(record, key, value)
        if errors:
            db.session.rollback()
        else:
            record.updated_by_user_id = current_user.id
            db.session.commit()
            flash("Einstellungen gespeichert.", "success")
            return redirect(url_for("calendar.settings"))
    return _render_settings(record, form=request.form if errors else None, errors=errors)


def _render_settings(record, *, form=None, errors=None, ob_form=None, ob_errors=None, status=200):
    from app.services import availability
    from app.services import office_booking as ob

    hours = availability.office_hours(record)
    page = ob.page_for_office()
    return render_template(
        "calendar/settings.html",
        record=record,
        form=form,
        errors=errors or [],
        days=[(iso, _DAY_LABELS[iso - 1], hours.get(iso)) for iso in range(1, 8)],
        ob_page=page, ob_form=ob_form, ob_errors=ob_errors or [], ob_windows=ob.windows_of(page) if page else {},
        ob_members=cal.office_members(), member_name=cal.member_name, ob_suggested=ob.suggested_slug(current_user),
        ob_base=(current_app.config.get("PUBLIC_URL") or request.url_root).rstrip("/"), ob_url=ob.public_url(page) if page else None,
    ), status


@calendar_bp.post("/einstellungen/online-buchung")
@login_required
def online_booking_settings():
    """Allgemeiner Buchungslink: aktivieren/deaktivieren, Adresse, Termintyp, Dauer, Kalender, Zeitfenster.
    Nur Buero-Admin."""
    from app.models import OfficeSettings
    from app.services import office_booking as ob

    if not current_user.is_office_admin:
        abort(403)
    try:
        page = ob.save_settings(current_user, request.form)
    except ob.BookingError as exc:
        db.session.rollback()
        return _render_settings(OfficeSettings.query.first(), ob_form=request.form, ob_errors=[str(exc)], status=422)
    flash("Online-Buchung gespeichert." + (" Der allgemeine Buchungslink ist aktiv." if page.enabled else " Der Link ist deaktiviert."), "success")
    return redirect(url_for("calendar.settings") + "#online-buchung")


# --- Persoenliche Buchungslinks (nur Buero-Admin) ------------------------------------------


def availability_settings():
    from app.services import availability

    return availability.settings()


def _links_allowed():
    """Buero-Admin oder Mitarbeiter mit Freigabe (users.can_send_booking_links) - serverseitig."""
    if not current_user.may_send_booking_links:
        abort(403)


def _link_assignees():
    """Admin: alle Mitarbeiter. Mitarbeiter mit Freigabe: nur Buero-Admins (deren Buerozeiten)."""
    members = cal.office_members()
    return members if current_user.is_office_admin else [m for m in members if m.is_office_admin]


@calendar_bp.get("/buchungslinks")
@login_required
def booking_links():
    from app.models import BookingLink, Customer
    from app.services import booking_links as bl

    _links_allowed()
    customer = None
    if request.args.get("kunde", type=int):
        customer = Customer.query.filter(Customer.id == request.args.get("kunde", type=int)).first()
    query = BookingLink.query
    if not current_user.is_office_admin:
        query = query.filter(BookingLink.created_by_user_id == current_user.id)
    links = query.order_by(BookingLink.created_at.desc(), BookingLink.id.desc()).limit(100).all()
    rows = []
    for link in links:
        event = bl.booked_event(link)
        rows.append({"link": link, "status": bl.status(link), "url": bl.public_url(link), "event": event,
                     "free": len(bl.free_times(link)) if bl.status(link) == "active" else 0})
    today = local_today()
    from app.services import office_booking as ob

    office_page = ob.page_for_office()
    office_link = ob.public_url(office_page) if office_page and ob.usable(office_page) else None
    return render_template(
        "calendar/booking_links.html", office_page=office_page, office_link=office_link,
        rows=rows, labels=bl.STATUS_LABELS, members=_link_assignees(), member_name=cal.member_name,
        kinds=cal.LOCATION_KINDS, customer=customer, new_id=request.args.get("neu", type=int),
        defaults={"from": (today + timedelta(days=1)).isoformat(), "to": (today + timedelta(days=7)).isoformat(),
                  "valid": (today + timedelta(days=14)).isoformat()},
        settings=availability_settings(),
    )


@calendar_bp.post("/buchungslinks")
@login_required
def booking_link_create():
    from datetime import time as _time

    from app.services import booking_links as bl
    from app.services.timetracking.clock import local_to_utc_naive

    _links_allowed()
    form = request.form
    back = url_for("calendar.booking_links", kunde=form.get("customer_id") or None)
    try:
        assignee_id = int(form.get("assignee_id") or 0)
        if not current_user.is_office_admin:
            # Mitarbeiter: Zeiten, Dauer und Gueltigkeit legt der Buero-Admin fest (Buerozeiten).
            assignee = next((m for m in _link_assignees() if m.id == assignee_id), None)
            if assignee is None:
                raise bl.LinkError("Bitte einen Büro-Admin auswählen.")
            s = availability_settings()
            minutes = int(s.default_minutes or 30) if s else 30
            offers, last_day = bl.offers_from_office_hours(assignee, minutes)
            link, _token = bl.create(
                current_user, title=form.get("title", ""), location_kind=form.get("location_kind") or None, minutes=minutes,
                assignee_id=assignee.id, customer_id=int(form["customer_id"]) if (form.get("customer_id") or "").isdigit() else None,
                offers=offers, valid_until=local_to_utc_naive(last_day, _time(23, 59)),
            )
            flash("Buchungslink erstellt. Link kopieren und an den Kunden senden.", "success")
            return redirect(url_for("calendar.booking_links", neu=link.id))
        if form.get("mode") == "range":
            first, last = date.fromisoformat(form.get("from", "")), date.fromisoformat(form.get("to", ""))
            weekdays = {int(d) for d in form.getlist("weekdays") if d.isdigit()}
            times = [t for t in re.split(r"[,;\s]+", form.get("times", "")) if t]
            if not weekdays or not times:
                raise bl.LinkError("Bitte Wochentage und mindestens eine Uhrzeit angeben.")
            offers = bl.offers_from_range(first, last, weekdays, times)
        else:
            offers = bl.offers_from_slots(form.getlist("slots"))
        valid_day = date.fromisoformat(form.get("valid_until", ""))
        valid_until = local_to_utc_naive(valid_day, _time(23, 59))
        link, _token = bl.create(
            current_user, title=form.get("title", ""), location_kind=form.get("location_kind") or None,
            minutes=int(form.get("minutes") or 30), assignee_id=assignee_id,
            customer_id=int(form["customer_id"]) if (form.get("customer_id") or "").isdigit() else None,
            offers=offers, valid_until=valid_until,
        )
    except (ValueError, bl.LinkError) as exc:
        flash(str(exc) if isinstance(exc, bl.LinkError) else "Bitte Datum, Uhrzeiten und Dauer vollständig angeben.", "error")
        return redirect(back)
    flash(f"Buchungslink erstellt ({len(link.offers)} freigegebene Uhrzeiten). Link kopieren und an den Kunden senden.", "success")
    return redirect(url_for("calendar.booking_links", neu=link.id))


@calendar_bp.post("/buchungslinks/<int:link_id>/widerrufen")
@login_required
def booking_link_revoke(link_id):
    from app.models import BookingLink
    from app.services import booking_links as bl

    _links_allowed()
    link = get_or_404_scoped(BookingLink, link_id)
    if not current_user.is_office_admin and link.created_by_user_id != current_user.id:
        abort(403)
    bl.revoke(current_user, link)
    flash("Buchungslink widerrufen. Er kann nicht mehr verwendet werden.", "success")
    return redirect(url_for("calendar.booking_links"))
