"""Zeiterfassung: Stempeluhr und eigene Auswertungen fuer alle Nutzer, Team-Verwaltung,
Korrekturen und Antraege nur fuer Admins (@admin_required).

Mandantentrennung: Alle Abfragen laufen ueber den globalen Tenant-Filter; Einzelobjekte
werden ausschliesslich per get_or_404_scoped geladen. Mitarbeiter-Ansichten filtern
zusaetzlich immer auf current_user.id."""

from datetime import date, datetime, timedelta

from flask import Blueprint, Response, abort, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from app.auth.permissions import admin_required
from app.extensions import db
from app.models import (
    CorrectionRequestStatus,
    Tenant,
    TimeCorrection,
    TimeCorrectionRequest,
    User,
    WorkBreak,
    WorkSession,
)
from app.models.audit_log import AuditEventType
from app.services.audit import log_audit_event
from app.services.timetracking import export, service
from app.services.timetracking.calc import month_bounds, week_bounds
from app.services.timetracking.clock import local_to_utc_naive, local_today, to_local, utcnow_naive
from app.services.timetracking.service import TimeTrackingError
from app.services.user_admin import get_office_member_or_404, office_members_query
from app.tenancy import get_or_404_scoped

timetracking_bp = Blueprint("timetracking", __name__, url_prefix="/zeiterfassung")

VIEWS = ("tag", "woche", "monat")


# --- Hilfsfunktionen -----------------------------------------------------------------------


def _parse_day(value: str | None, default: date) -> date:
    try:
        return date.fromisoformat(value) if value else default
    except ValueError:
        return default


def _parse_month(value: str | None, default: date) -> date:
    try:
        return datetime.strptime(value, "%Y-%m").date() if value else default.replace(day=1)
    except ValueError:
        return default.replace(day=1)


def _parse_local(day_value: str | None, time_value: str | None, label: str) -> datetime:
    try:
        day = date.fromisoformat((day_value or "").strip())
        clock_time = datetime.strptime((time_value or "").strip(), "%H:%M").time()
    except ValueError as exc:
        raise TimeTrackingError(f"Bitte {label} vollständig angeben (Datum und Uhrzeit).") from exc
    return local_to_utc_naive(day, clock_time)


def _parse_optional_end(form, prefix: str = "end") -> datetime | None:
    if not (form.get(f"{prefix}_time") or "").strip():
        return None
    return _parse_local(form.get(f"{prefix}_date"), form.get(f"{prefix}_time"), "das Ende")


def _form_values(started_at: datetime | None, ended_at: datetime | None, fallback_day: date) -> dict:
    start_local, end_local = to_local(started_at), to_local(ended_at)
    return {
        "start_date": (start_local.date() if start_local else fallback_day).isoformat(),
        "start_time": start_local.strftime("%H:%M") if start_local else "",
        "end_date": (end_local.date() if end_local else (start_local.date() if start_local else fallback_day)).isoformat(),
        "end_time": end_local.strftime("%H:%M") if end_local else "",
    }


def _period_for(view: str, anchor: date) -> tuple[date, date]:
    if view == "woche":
        return week_bounds(anchor)
    if view == "monat":
        return month_bounds(anchor)
    return anchor, anchor


def _shift(view: str, anchor: date, direction: int) -> date:
    if view == "woche":
        return anchor + timedelta(days=7 * direction)
    if view == "monat":
        first = anchor.replace(day=1)
        if direction < 0:
            return (first - timedelta(days=1)).replace(day=1)
        return (first + timedelta(days=32)).replace(day=1)
    return anchor + timedelta(days=direction)


def _audit_time(event_type: AuditEventType, details: dict) -> None:
    """Schliesst die Transaktion (Aenderung + TimeCorrection + Audit) gemeinsam ab."""
    log_audit_event(event_type, user=current_user, details=details)


def _employee_or_404(user_id: int) -> User:
    # Nur Buero-Mitglieder des eigenen Mandanten; geloeschte Konten bleiben fuer die Historie
    # erreichbar, SUPER_ADMIN-Konten nie.
    return get_office_member_or_404(user_id, include_deleted=True)


def _request_link(day: date) -> str:
    return url_for("timetracking.new_request", datum=day.isoformat())


def _pending_request_count() -> int:
    return TimeCorrectionRequest.query.filter_by(status=CorrectionRequestStatus.PENDING).count()


# --- Stempeluhr (alle Rollen, nur eigene Daten) --------------------------------------------


@timetracking_bp.get("")
@login_required
def index():
    now = utcnow_naive()
    today = local_today(now)
    state = service.get_stamp_state(current_user.id, now)
    week = service.summarize_user_period(current_user, *week_bounds(today), now=now)
    month = service.summarize_user_period(current_user, *month_bounds(today), now=now)
    today_summary = next(item for item in week.days if item.day == today)
    return render_template(
        "timetracking/index.html",
        state=state,
        today=today,
        today_summary=today_summary,
        week=week,
        month=month,
        now=now,
    )


def _stamp(action, success_message: str, event_type: AuditEventType | None = None):
    try:
        action(current_user)
    except TimeTrackingError as exc:
        flash(str(exc), "error")
    else:
        if event_type is not None:
            # Aktivitaetsprotokoll des Bueros: nur das Ereignis, keine weiteren Daten.
            log_audit_event(event_type, user=current_user)
        flash(success_message, "success")
    return redirect(_safe_next() or url_for("timetracking.index"))


def _safe_next() -> str | None:
    """Rueckkehr z. B. zur Uebersicht nach dem Stempeln - nur relative Pfade dieser Anwendung."""
    target = request.form.get("next") or ""
    if target.startswith("/") and not target.startswith("//") and "\\" not in target:
        return target
    return None


@timetracking_bp.post("/einstempeln")
@login_required
def clock_in():
    return _stamp(service.clock_in, "Sie sind eingestempelt.", AuditEventType.TIME_CLOCK_IN)


@timetracking_bp.post("/ausstempeln")
@login_required
def clock_out():
    return _stamp(service.clock_out, "Sie sind ausgestempelt.", AuditEventType.TIME_CLOCK_OUT)


@timetracking_bp.post("/pause/start")
@login_required
def break_start():
    return _stamp(service.start_break, "Pause gestartet.")


@timetracking_bp.post("/pause/ende")
@login_required
def break_end():
    return _stamp(service.end_break, "Pause beendet.")


@timetracking_bp.get("/woche")
@login_required
def week():
    today = local_today()
    anchor = _parse_day(request.args.get("datum"), today)
    start, end = week_bounds(anchor)
    return render_template(
        "timetracking/my_period.html",
        view="woche",
        summary=service.summarize_user_period(current_user, start, end),
        today=today,
        prev_url=url_for("timetracking.week", datum=_shift("woche", start, -1).isoformat()),
        next_url=url_for("timetracking.week", datum=_shift("woche", start, 1).isoformat()),
        current_url=url_for("timetracking.week"),
        request_link=_request_link,
    )


@timetracking_bp.get("/monat")
@login_required
def month():
    today = local_today()
    anchor = _parse_month(request.args.get("monat"), today)
    start, end = month_bounds(anchor)
    return render_template(
        "timetracking/my_period.html",
        view="monat",
        summary=service.summarize_user_period(current_user, start, end),
        today=today,
        prev_url=url_for("timetracking.month", monat=_shift("monat", start, -1).strftime("%Y-%m")),
        next_url=url_for("timetracking.month", monat=_shift("monat", start, 1).strftime("%Y-%m")),
        current_url=url_for("timetracking.month"),
        request_link=_request_link,
    )


# --- Korrekturantraege (Mitarbeiter: nur eigene) -------------------------------------------


@timetracking_bp.get("/korrekturantraege")
@login_required
def my_requests():
    requests_ = (
        TimeCorrectionRequest.query.filter_by(user_id=current_user.id)
        .order_by(TimeCorrectionRequest.created_at.desc())
        .all()
    )
    return render_template("timetracking/my_requests.html", correction_requests=requests_)


@timetracking_bp.route("/korrekturantraege/neu", methods=["GET", "POST"])
@login_required
def new_request():
    today = local_today()
    session_id = request.values.get("buchung", type=int)
    work_session = None
    if session_id is not None:
        work_session = get_or_404_scoped(WorkSession, session_id)
        if work_session.user_id != current_user.id:
            abort(404)
    fallback_day = _parse_day(request.values.get("datum"), today)
    values = _form_values(
        work_session.started_at if work_session else None,
        work_session.ended_at if work_session else None,
        work_session.work_date if work_session else fallback_day,
    )
    reason = ""

    if request.method == "POST":
        values = {key: request.form.get(key, "") for key in ("start_date", "start_time", "end_date", "end_time")}
        reason = request.form.get("reason", "")
        try:
            started_at = _parse_local(values["start_date"], values["start_time"], "den Beginn")
            ended_at = _parse_local(values["end_date"], values["end_time"], "das Ende")
            correction_request = service.create_correction_request(
                current_user, work_session, started_at, ended_at, reason
            )
            db.session.flush()
            _audit_time(
                AuditEventType.TIME_CORRECTION_REQUESTED,
                {"request_id": correction_request.id, "work_session_id": session_id},
            )
        except TimeTrackingError as exc:
            db.session.rollback()
            flash(str(exc), "error")
        else:
            flash("Ihr Korrekturantrag wurde eingereicht. Ein Administrator prüft ihn.", "success")
            return redirect(url_for("timetracking.my_requests"))

    return render_template(
        "timetracking/request_form.html",
        work_session=work_session,
        values=values,
        reason=reason,
    )


# --- Admin: Team, Mitarbeiterdetail --------------------------------------------------------


@timetracking_bp.get("/team")
@login_required
@admin_required
def team():
    users = office_members_query().filter(User.is_active.is_(True)).order_by(User.email).all()
    rows = service.build_team_overview(users)
    rows.sort(key=lambda row: (row.user.employee_profile.display_name if row.user.employee_profile and row.user.employee_profile.display_name else row.user.email).lower())
    return render_template(
        "timetracking/team.html",
        rows=rows,
        today=local_today(),
        pending_count=_pending_request_count(),
    )


@timetracking_bp.get("/team/<int:user_id>")
@login_required
@admin_required
def employee(user_id):
    employee_user = _employee_or_404(user_id)
    today = local_today()
    view = request.args.get("ansicht", "woche")
    if view not in VIEWS:
        view = "woche"
    anchor = _parse_day(request.args.get("datum"), today)
    start, end = _period_for(view, anchor)
    summary = service.summarize_user_period(employee_user, start, end)

    day_sessions = []
    corrections = []
    if view == "tag":
        day_sessions = service.sessions_between([employee_user.id], start, end, include_voided=True)
        session_ids = [item.id for item in day_sessions]
        break_ids = [br.id for item in day_sessions for br in item.breaks]
        if session_ids:
            corrections = (
                TimeCorrection.query.filter(
                    TimeCorrection.user_id == employee_user.id,
                    db.or_(
                        db.and_(TimeCorrection.target_type == "session", TimeCorrection.target_id.in_(session_ids)),
                        db.and_(TimeCorrection.target_type == "break", TimeCorrection.target_id.in_(break_ids or [-1])),
                    ),
                )
                .order_by(TimeCorrection.created_at.desc())
                .all()
            )

    def link(target_view, target_day):
        return url_for("timetracking.employee", user_id=employee_user.id, ansicht=target_view, datum=target_day.isoformat())

    return render_template(
        "timetracking/employee.html",
        employee=employee_user,
        view=view,
        anchor=anchor,
        summary=summary,
        today=today,
        day_sessions=day_sessions,
        corrections=corrections,
        state=service.get_stamp_state(employee_user.id),
        prev_url=link(view, _shift(view, start, -1)),
        next_url=link(view, _shift(view, start, 1)),
        today_url=link(view, today),
        view_links={key: link(key, anchor) for key in VIEWS},
        day_link=lambda target_day: link("tag", target_day),
    )


# --- Admin: Monatsexport (Lohnabrechnung / Archiv) -------------------------------------------


def _export_month() -> date:
    return _parse_month(request.args.get("monat"), local_today())


def _download(data: bytes, filename: str, mimetype: str) -> Response:
    response = Response(data, mimetype=mimetype)
    response.headers["Content-Disposition"] = f'attachment; filename="{filename}"'
    response.headers["Cache-Control"] = "no-store"
    return response


def _export_name(user: User) -> tuple[str, str | None]:
    profile = user.employee_profile
    name = profile.display_name if profile and profile.display_name else user.email
    return name, profile.personnel_number if profile else None


def _file_slug(value: str) -> str:
    return "".join(ch if ch.isalnum() else "-" for ch in value.split("@")[0]).strip("-").lower() or "mitarbeiter"


@timetracking_bp.get("/team/<int:user_id>/export.<fmt>")
@login_required
@admin_required
def employee_export(user_id, fmt):
    if fmt not in ("csv", "pdf"):
        abort(404)
    employee_user = _employee_or_404(user_id)
    month_start, month_end = month_bounds(_export_month())
    summary = service.summarize_user_period(employee_user, month_start, month_end)
    name, personnel_number = _export_name(employee_user)
    filename = f"arbeitszeiten-{_file_slug(name)}-{month_start.strftime('%Y-%m')}.{fmt}"
    if fmt == "csv":
        return _download(export.month_csv(name, personnel_number, summary, local_today()), filename, "text/csv; charset=utf-8")
    tenant = db.session.get(Tenant, current_user.tenant_id)
    data = export.month_pdf(tenant.name if tenant else "", name, personnel_number, summary, local_today())
    return _download(data, filename, "application/pdf")


@timetracking_bp.get("/team/export.csv")
@login_required
@admin_required
def team_export():
    month_start, month_end = month_bounds(_export_month())
    users = office_members_query(include_deleted=False).order_by(User.email).all()
    rows = []
    for user in users:
        summary = service.summarize_user_period(user, month_start, month_end)
        if not user.is_active and not summary.net_seconds:
            continue
        name, personnel_number = _export_name(user)
        rows.append((name, personnel_number, summary))
    rows.sort(key=lambda row: row[0].lower())
    filename = f"arbeitszeiten-buero-{month_start.strftime('%Y-%m')}.csv"
    return _download(export.team_csv(rows, month_start), filename, "text/csv; charset=utf-8")


# --- Admin: Korrekturen an Buchungen und Pausen --------------------------------------------


def _redirect_to_day(work_session: WorkSession):
    return redirect(
        url_for("timetracking.employee", user_id=work_session.user_id, ansicht="tag", datum=work_session.work_date.isoformat())
    )


@timetracking_bp.route("/team/<int:user_id>/buchungen/neu", methods=["GET", "POST"])
@login_required
@admin_required
def session_create(user_id):
    employee_user = _employee_or_404(user_id)
    day = _parse_day(request.values.get("datum"), local_today())
    values = _form_values(None, None, day)
    reason = ""
    if request.method == "POST":
        values = {key: request.form.get(key, "") for key in ("start_date", "start_time", "end_date", "end_time")}
        reason = request.form.get("reason", "")
        try:
            started_at = _parse_local(values["start_date"], values["start_time"], "den Beginn")
            ended_at = _parse_local(values["end_date"], values["end_time"], "das Ende")
            work_session = service.create_manual_session(employee_user, started_at, ended_at, current_user, reason)
            _audit_time(
                AuditEventType.TIME_CORRECTED,
                {"action": "create", "work_session_id": work_session.id, "employee_user_id": employee_user.id},
            )
        except TimeTrackingError as exc:
            db.session.rollback()
            flash(str(exc), "error")
        else:
            flash("Die Buchung wurde nachgetragen und protokolliert.", "success")
            return _redirect_to_day(work_session)
    return render_template(
        "timetracking/entry_form.html",
        employee=employee_user,
        mode="session_create",
        title="Buchung nachtragen",
        values=values,
        reason=reason,
        allow_open_end=False,
        cancel_url=url_for("timetracking.employee", user_id=employee_user.id, ansicht="tag", datum=day.isoformat()),
    )


@timetracking_bp.route("/buchungen/<int:session_id>/bearbeiten", methods=["GET", "POST"])
@login_required
@admin_required
def session_edit(session_id):
    work_session = get_or_404_scoped(WorkSession, session_id)
    employee_user = _employee_or_404(work_session.user_id)
    values = _form_values(work_session.started_at, work_session.ended_at, work_session.work_date)
    reason = ""
    if request.method == "POST":
        values = {key: request.form.get(key, "") for key in ("start_date", "start_time", "end_date", "end_time")}
        reason = request.form.get("reason", "")
        try:
            started_at = _parse_local(values["start_date"], values["start_time"], "den Beginn")
            ended_at = _parse_optional_end(request.form)
            changed = service.update_session_times(work_session, started_at, ended_at, current_user, reason)
            if changed:
                _audit_time(
                    AuditEventType.TIME_CORRECTED,
                    {"action": "update", "work_session_id": work_session.id, "employee_user_id": employee_user.id},
                )
        except TimeTrackingError as exc:
            db.session.rollback()
            flash(str(exc), "error")
        else:
            if changed:
                flash("Die Buchung wurde korrigiert. Der ursprüngliche Wert bleibt im Protokoll erhalten.", "success")
            else:
                db.session.rollback()
                flash("Keine Änderung – es wurde nichts gespeichert.", "info")
            return _redirect_to_day(work_session)
    return render_template(
        "timetracking/entry_form.html",
        employee=employee_user,
        mode="session_edit",
        title="Buchung korrigieren",
        entry=work_session,
        values=values,
        reason=reason,
        allow_open_end=work_session.is_open,
        cancel_url=url_for("timetracking.employee", user_id=employee_user.id, ansicht="tag", datum=work_session.work_date.isoformat()),
    )


@timetracking_bp.post("/buchungen/<int:session_id>/stornieren")
@login_required
@admin_required
def session_void(session_id):
    work_session = get_or_404_scoped(WorkSession, session_id)
    try:
        service.void_session(work_session, current_user, request.form.get("reason"))
        _audit_time(
            AuditEventType.TIME_CORRECTED,
            {"action": "void", "work_session_id": work_session.id, "employee_user_id": work_session.user_id},
        )
    except TimeTrackingError as exc:
        db.session.rollback()
        flash(str(exc), "error")
    else:
        flash("Die Buchung wurde storniert. Sie bleibt im Protokoll sichtbar.", "success")
    return _redirect_to_day(work_session)


@timetracking_bp.route("/buchungen/<int:session_id>/pausen/neu", methods=["GET", "POST"])
@login_required
@admin_required
def break_create(session_id):
    work_session = get_or_404_scoped(WorkSession, session_id)
    employee_user = _employee_or_404(work_session.user_id)
    values = _form_values(None, None, work_session.work_date)
    reason = ""
    if request.method == "POST":
        values = {key: request.form.get(key, "") for key in ("start_date", "start_time", "end_date", "end_time")}
        reason = request.form.get("reason", "")
        try:
            started_at = _parse_local(values["start_date"], values["start_time"], "den Pausenbeginn")
            ended_at = _parse_local(values["end_date"], values["end_time"], "das Pausenende")
            work_break = service.add_break(work_session, started_at, ended_at, current_user, reason)
            _audit_time(
                AuditEventType.TIME_CORRECTED,
                {"action": "create_break", "work_break_id": work_break.id, "employee_user_id": employee_user.id},
            )
        except TimeTrackingError as exc:
            db.session.rollback()
            flash(str(exc), "error")
        else:
            flash("Die Pause wurde nachgetragen und protokolliert.", "success")
            return _redirect_to_day(work_session)
    return render_template(
        "timetracking/entry_form.html",
        employee=employee_user,
        mode="break_create",
        title="Pause nachtragen",
        entry=work_session,
        values=values,
        reason=reason,
        allow_open_end=False,
        cancel_url=url_for("timetracking.employee", user_id=employee_user.id, ansicht="tag", datum=work_session.work_date.isoformat()),
    )


@timetracking_bp.route("/pausen/<int:break_id>/bearbeiten", methods=["GET", "POST"])
@login_required
@admin_required
def break_edit(break_id):
    work_break = get_or_404_scoped(WorkBreak, break_id)
    work_session = work_break.work_session
    employee_user = _employee_or_404(work_break.user_id)
    values = _form_values(work_break.started_at, work_break.ended_at, work_session.work_date)
    reason = ""
    if request.method == "POST":
        values = {key: request.form.get(key, "") for key in ("start_date", "start_time", "end_date", "end_time")}
        reason = request.form.get("reason", "")
        try:
            started_at = _parse_local(values["start_date"], values["start_time"], "den Pausenbeginn")
            ended_at = _parse_local(values["end_date"], values["end_time"], "das Pausenende")
            changed = service.update_break_times(work_break, started_at, ended_at, current_user, reason)
            if changed:
                _audit_time(
                    AuditEventType.TIME_CORRECTED,
                    {"action": "update_break", "work_break_id": work_break.id, "employee_user_id": employee_user.id},
                )
        except TimeTrackingError as exc:
            db.session.rollback()
            flash(str(exc), "error")
        else:
            if changed:
                flash("Die Pause wurde korrigiert und protokolliert.", "success")
            else:
                db.session.rollback()
                flash("Keine Änderung – es wurde nichts gespeichert.", "info")
            return _redirect_to_day(work_session)
    return render_template(
        "timetracking/entry_form.html",
        employee=employee_user,
        mode="break_edit",
        title="Pause korrigieren",
        entry=work_break,
        values=values,
        reason=reason,
        allow_open_end=False,
        cancel_url=url_for("timetracking.employee", user_id=employee_user.id, ansicht="tag", datum=work_session.work_date.isoformat()),
    )


@timetracking_bp.post("/pausen/<int:break_id>/stornieren")
@login_required
@admin_required
def break_void(break_id):
    work_break = get_or_404_scoped(WorkBreak, break_id)
    try:
        service.void_break(work_break, current_user, request.form.get("reason"))
        _audit_time(
            AuditEventType.TIME_CORRECTED,
            {"action": "void_break", "work_break_id": work_break.id, "employee_user_id": work_break.user_id},
        )
    except TimeTrackingError as exc:
        db.session.rollback()
        flash(str(exc), "error")
    else:
        flash("Die Pause wurde storniert.", "success")
    return _redirect_to_day(work_break.work_session)


# --- Admin: Antraege und Protokoll ---------------------------------------------------------


@timetracking_bp.get("/antraege")
@login_required
@admin_required
def requests():
    status_filter = request.args.get("status", "offen")
    query = TimeCorrectionRequest.query.order_by(TimeCorrectionRequest.created_at.desc())
    if status_filter == "offen":
        query = query.filter_by(status=CorrectionRequestStatus.PENDING)
    else:
        status_filter = "alle"
    return render_template(
        "timetracking/requests.html",
        correction_requests=query.limit(200).all(),
        status_filter=status_filter,
        pending_count=_pending_request_count(),
    )


@timetracking_bp.post("/antraege/<int:request_id>/entscheiden")
@login_required
@admin_required
def decide_request(request_id):
    correction_request = get_or_404_scoped(TimeCorrectionRequest, request_id)
    decision = request.form.get("decision")
    note = request.form.get("note")
    try:
        if decision == "approve":
            service.approve_correction_request(correction_request, current_user, note)
        elif decision == "reject":
            service.reject_correction_request(correction_request, current_user, note)
        else:
            raise TimeTrackingError("Unbekannte Entscheidung.")
        _audit_time(
            AuditEventType.TIME_CORRECTION_DECIDED,
            {"request_id": correction_request.id, "decision": decision, "employee_user_id": correction_request.user_id},
        )
    except TimeTrackingError as exc:
        db.session.rollback()
        flash(f"Antrag #{request_id}: {exc}", "error")
    else:
        flash(
            f"Antrag #{request_id} wurde {'genehmigt und übernommen' if decision == 'approve' else 'abgelehnt'}.",
            "success",
        )
    return redirect(url_for("timetracking.requests"))


@timetracking_bp.get("/protokoll")
@login_required
@admin_required
def audit():
    user_filter = request.args.get("mitarbeiter", type=int)
    query = TimeCorrection.query.order_by(TimeCorrection.created_at.desc(), TimeCorrection.id.desc())
    if user_filter:
        query = query.filter(TimeCorrection.user_id == user_filter)
    users = office_members_query(include_deleted=True).order_by(User.email).all()
    return render_template(
        "timetracking/audit.html",
        corrections=query.limit(500).all(),
        users=users,
        user_filter=user_filter,
    )
