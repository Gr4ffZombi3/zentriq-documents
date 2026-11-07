"""Gemeinsamer Buerokalender (Seite + JSON-API).

Zugriff: Buero-Admins und Mitarbeiter des eigenen Bueros (SUPER_ADMIN nie). Mandantentrennung
ueber den globalen Tenant-Filter; Termine werden per get_or_404_scoped geladen - Termine anderer
Bueros sind fuer jede Aktion 404. Loeschen: Ersteller oder Buero-Admin; absagen/aendern: alle."""

import re
from datetime import date, timedelta

from flask import Blueprint, abort, flash, jsonify, redirect, render_template, request, url_for
from flask_login import current_user, login_required

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


@calendar_bp.post("/api/termine")
@login_required
def api_create():
    _office_member()
    data = _payload()
    try:
        event, created, conflicts = cal.create_event(current_user, data, client_ref=data.get("client_ref"))
    except cal.CalendarError as exc:
        return _error(exc)
    return jsonify(
        {"event": cal.serialize(event, current_user), "created": created, "conflicts": cal.conflict_payload(conflicts)}
    ), (201 if created else 200)


@calendar_bp.patch("/api/termine/<int:event_id>")
@login_required
def api_update(event_id):
    _office_member()
    event = get_or_404_scoped(CalendarEvent, event_id)
    try:
        event, changed, conflicts = cal.update_event(current_user, event, _payload())
    except cal.CalendarError as exc:
        if exc.code == "conflict":
            fresh = get_or_404_scoped(CalendarEvent, event_id)
            return jsonify({"error": str(exc), "code": exc.code, "event": cal.serialize(fresh, current_user)}), 409
        return _error(exc)
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
    hours = availability.office_hours(record)
    return render_template(
        "calendar/settings.html",
        record=record,
        form=request.form if errors else None,
        errors=errors,
        days=[(iso, _DAY_LABELS[iso - 1], hours.get(iso)) for iso in range(1, 8)],
    )
