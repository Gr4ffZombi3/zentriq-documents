"""Geschaeftslogik der Zeiterfassung: Stempeln, Korrekturen, Korrekturantraege, Auswertungen.

Alle Zeitstempel beim Stempeln kommen von `utcnow_naive()` - der Client kann keine Zeit
vorgeben. Korrekturen aendern Buchungen nur zusammen mit TimeCorrection-Eintraegen
(alter/neuer Wert, Akteur, Zeitpunkt, Begruendung). Funktionen fuer Korrekturen und Antraege
committen NICHT selbst; der Aufrufer schliesst die Transaktion (typischerweise ueber
`log_audit_event`, das committet), damit Aenderung und Protokoll atomar gespeichert werden.
"""

from dataclasses import dataclass
from datetime import date, datetime, timedelta

from flask import has_request_context, request
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload

from app.extensions import db
from app.models import (
    CorrectionRequestStatus,
    EmployeeProfile,
    TimeCorrection,
    TimeCorrectionRequest,
    TimeEntrySource,
    User,
    WorkBreak,
    WorkSession,
)
from app.services.timetracking.calc import (
    PeriodSummary,
    month_bounds,
    session_end,
    summarize_period,
    week_bounds,
)
from app.services.timetracking.clock import format_utc_iso, local_date_of, local_today, utcnow_naive

# Offene Buchungen, die laenger laufen, gelten als "Ausstempeln vergessen". Sie werden beim
# naechsten Einstempeln ohne erfundene Endzeit geloest und muessen korrigiert werden.
STALE_SESSION_HOURS = 16


class TimeTrackingError(Exception):
    """Fachlicher Fehler mit einer fuer Anwender verstaendlichen Meldung."""


# --- Abfragen ------------------------------------------------------------------------------


def get_open_session(user_id: int) -> WorkSession | None:
    return WorkSession.query.filter_by(user_id=user_id, open_marker=1).first()


def get_open_break(user_id: int) -> WorkBreak | None:
    return WorkBreak.query.filter_by(user_id=user_id, open_marker=1).first()


def is_stale(session: WorkSession | None, now: datetime) -> bool:
    return (
        session is not None
        and session.is_open
        and now - session.started_at > timedelta(hours=STALE_SESSION_HOURS)
    )


@dataclass
class StampState:
    open_session: WorkSession | None
    open_break: WorkBreak | None
    stale: bool

    @property
    def status(self) -> str:
        if self.open_session is None or self.stale:
            return "out"
        return "break" if self.open_break is not None else "in"


def get_stamp_state(user_id: int, now: datetime | None = None) -> StampState:
    now = now or utcnow_naive()
    open_session = get_open_session(user_id)
    return StampState(
        open_session=open_session,
        open_break=get_open_break(user_id),
        stale=is_stale(open_session, now),
    )


def sessions_between(user_ids, start: date, end: date, include_voided: bool = False) -> list[WorkSession]:
    query = (
        WorkSession.query.options(selectinload(WorkSession.breaks))
        .filter(WorkSession.user_id.in_(list(user_ids)), WorkSession.work_date.between(start, end))
        .order_by(WorkSession.started_at)
    )
    if not include_voided:
        query = query.filter(WorkSession.voided_at.is_(None))
    return query.all()


def summarize_user_period(user: User, start: date, end: date, now: datetime | None = None) -> PeriodSummary:
    now = now or utcnow_naive()
    sessions = sessions_between([user.id], start, end)
    return summarize_period(sessions, start, end, user.employee_profile, local_today(now), now)


@dataclass
class TeamRow:
    user: User
    state: str
    today: object
    week: PeriodSummary
    month: PeriodSummary


def build_team_overview(users: list[User], now: datetime | None = None) -> list[TeamRow]:
    """Eine Abfrage fuer alle Buchungen des relevanten Zeitraums statt einer pro Person."""
    now = now or utcnow_naive()
    today = local_today(now)
    week_start, week_end = week_bounds(today)
    month_start, month_end = month_bounds(today)
    range_start, range_end = min(week_start, month_start), max(week_end, month_end)

    user_ids = [item.id for item in users]
    sessions = sessions_between(user_ids, range_start, range_end) if user_ids else []
    by_user: dict[int, list] = {}
    for session in sessions:
        by_user.setdefault(session.user_id, []).append(session)

    open_breaks = {
        item.user_id for item in WorkBreak.query.filter(WorkBreak.open_marker == 1).all()
    } if user_ids else set()

    rows = []
    for user in users:
        user_sessions = by_user.get(user.id, [])
        profile = user.employee_profile
        week = summarize_period(user_sessions, week_start, week_end, profile, today, now)
        month = summarize_period(user_sessions, month_start, month_end, profile, today, now)
        today_summary = next(item for item in week.days if item.day == today)
        running = [item for item in user_sessions if item.is_open and not is_stale(item, now)]
        if running:
            state = "break" if user.id in open_breaks else "in"
        else:
            state = "out"
        rows.append(TeamRow(user=user, state=state, today=today_summary, week=week, month=month))
    return rows


# --- Stempeln (Serverzeit) -----------------------------------------------------------------


def _commit_or_conflict(message: str) -> None:
    try:
        db.session.commit()
    except IntegrityError as exc:
        db.session.rollback()
        raise TimeTrackingError(message) from exc


def _release_stale_session(user: User, now: datetime) -> None:
    session = get_open_session(user.id)
    if not is_stale(session, now):
        return
    session.open_marker = None
    for work_break in session.breaks:
        if work_break.open_marker == 1:
            work_break.open_marker = None
    db.session.add(
        TimeCorrection(
            tenant_id=session.tenant_id,
            user_id=session.user_id,
            target_type="session",
            target_id=session.id,
            action="release",
            reason="Ausstempeln fehlte – Buchung ohne Endzeit zur Korrektur markiert.",
        )
    )


def clock_in(user: User) -> WorkSession:
    now = utcnow_naive()
    _release_stale_session(user, now)
    if get_open_session(user.id) is not None:
        raise TimeTrackingError("Sie sind bereits eingestempelt.")
    session = WorkSession(
        tenant_id=user.tenant_id,
        user_id=user.id,
        work_date=local_date_of(now),
        started_at=now,
        open_marker=1,
        source=TimeEntrySource.STAMP,
        created_by_user_id=user.id,
    )
    db.session.add(session)
    _commit_or_conflict("Sie sind bereits eingestempelt.")
    return session


def _require_active_session(user: User, now: datetime) -> WorkSession:
    session = get_open_session(user.id)
    if session is None:
        raise TimeTrackingError("Sie sind derzeit nicht eingestempelt.")
    if is_stale(session, now):
        raise TimeTrackingError(
            "Die offene Buchung liegt zu lange zurück. Bitte einstempeln und für die alte Buchung "
            "einen Korrekturantrag stellen."
        )
    return session


def clock_out(user: User) -> WorkSession:
    now = utcnow_naive()
    session = _require_active_session(user, now)
    open_break = get_open_break(user.id)
    if open_break is not None:
        open_break.ended_at = now
        open_break.open_marker = None
    session.ended_at = now
    session.open_marker = None
    _commit_or_conflict("Die Buchung wurde bereits geändert. Bitte Seite neu laden.")
    return session


def start_break(user: User) -> WorkBreak:
    now = utcnow_naive()
    session = _require_active_session(user, now)
    if get_open_break(user.id) is not None:
        raise TimeTrackingError("Die Pause läuft bereits.")
    work_break = WorkBreak(
        tenant_id=user.tenant_id,
        work_session_id=session.id,
        user_id=user.id,
        started_at=now,
        open_marker=1,
    )
    db.session.add(work_break)
    _commit_or_conflict("Die Pause läuft bereits.")
    return work_break


def end_break(user: User) -> WorkBreak:
    now = utcnow_naive()
    _require_active_session(user, now)
    work_break = get_open_break(user.id)
    if work_break is None:
        raise TimeTrackingError("Es läuft keine Pause.")
    work_break.ended_at = now
    work_break.open_marker = None
    _commit_or_conflict("Die Pause wurde bereits beendet.")
    return work_break


# --- Korrekturen (nur Admin) ---------------------------------------------------------------


def _record(
    actor: User | None,
    target,
    target_type: str,
    action: str,
    field: str | None = None,
    old=None,
    new=None,
    reason: str | None = None,
    correction_request: TimeCorrectionRequest | None = None,
) -> None:
    def _as_text(value):
        if isinstance(value, datetime):
            return format_utc_iso(value)
        if isinstance(value, date):
            return value.isoformat()
        return None if value is None else str(value)

    db.session.add(
        TimeCorrection(
            tenant_id=target.tenant_id,
            user_id=target.user_id,
            target_type=target_type,
            target_id=target.id,
            action=action,
            field=field,
            old_value=_as_text(old),
            new_value=_as_text(new),
            reason=(reason or "").strip() or None,
            request_id=correction_request.id if correction_request is not None else None,
            corrected_by_user_id=actor.id if actor is not None else None,
            corrected_by_email_snapshot=actor.email if actor is not None else None,
            ip_address=request.remote_addr if has_request_context() else None,
        )
    )


def _validate_range(started_at: datetime, ended_at: datetime | None, now: datetime) -> None:
    if started_at > now or (ended_at is not None and ended_at > now):
        raise TimeTrackingError("Zeiten in der Zukunft sind nicht zulässig.")
    if ended_at is not None and ended_at <= started_at:
        raise TimeTrackingError("Das Ende muss nach dem Beginn liegen.")
    if ended_at is not None and ended_at - started_at > timedelta(hours=24):
        raise TimeTrackingError("Eine Buchung darf höchstens 24 Stunden umfassen.")


def _check_session_overlap(user_id: int, started_at: datetime, ended_at: datetime | None, now: datetime, exclude_id=None):
    end = ended_at or now
    candidates = WorkSession.query.filter(
        WorkSession.user_id == user_id,
        WorkSession.voided_at.is_(None),
        WorkSession.started_at < end,
        WorkSession.work_date >= local_date_of(started_at) - timedelta(days=2),
    ).all()
    for other in candidates:
        if other.id == exclude_id:
            continue
        other_end = session_end(other, now) or other.started_at
        if other.started_at < end and other_end > started_at:
            raise TimeTrackingError("Die Zeiten überschneiden sich mit einer anderen Buchung.")


def _check_breaks_inside(session: WorkSession, started_at: datetime, ended_at: datetime | None) -> None:
    for work_break in session.active_breaks:
        if work_break.started_at < started_at or (
            ended_at is not None and (work_break.ended_at is None or work_break.ended_at > ended_at)
        ):
            raise TimeTrackingError(
                "Pausen müssen innerhalb der Buchung liegen. Bitte zuerst die betroffene Pause korrigieren."
            )


def update_session_times(
    session: WorkSession,
    started_at: datetime,
    ended_at: datetime | None,
    actor: User,
    reason: str | None = None,
    correction_request: TimeCorrectionRequest | None = None,
) -> bool:
    """Gibt False zurueck, wenn sich nichts geaendert hat."""
    now = utcnow_naive()
    if session.voided_at is not None:
        raise TimeTrackingError("Stornierte Buchungen können nicht geändert werden.")
    if ended_at is None and not session.is_open:
        raise TimeTrackingError("Bitte ein Ende angeben.")
    _validate_range(started_at, ended_at, now)
    _check_session_overlap(session.user_id, started_at, ended_at, now, exclude_id=session.id)
    _check_breaks_inside(session, started_at, ended_at)

    changed = False
    if started_at != session.started_at:
        _record(actor, session, "session", "update", "started_at", session.started_at, started_at, reason, correction_request)
        session.started_at = started_at
        changed = True
    new_work_date = local_date_of(started_at)
    if new_work_date != session.work_date:
        _record(actor, session, "session", "update", "work_date", session.work_date, new_work_date, reason, correction_request)
        session.work_date = new_work_date
    if ended_at != session.ended_at:
        _record(actor, session, "session", "update", "ended_at", session.ended_at, ended_at, reason, correction_request)
        session.ended_at = ended_at
        changed = True
    if session.ended_at is not None:
        session.open_marker = None
    if changed:
        session.is_corrected = True
    return changed


def create_manual_session(
    employee: User,
    started_at: datetime,
    ended_at: datetime,
    actor: User,
    reason: str | None = None,
    correction_request: TimeCorrectionRequest | None = None,
) -> WorkSession:
    now = utcnow_naive()
    _validate_range(started_at, ended_at, now)
    _check_session_overlap(employee.id, started_at, ended_at, now)
    session = WorkSession(
        tenant_id=employee.tenant_id,
        user_id=employee.id,
        work_date=local_date_of(started_at),
        started_at=started_at,
        ended_at=ended_at,
        source=TimeEntrySource.MANUAL,
        is_corrected=True,
        created_by_user_id=actor.id,
    )
    db.session.add(session)
    db.session.flush()
    _record(actor, session, "session", "create", "started_at", None, started_at, reason, correction_request)
    _record(actor, session, "session", "create", "ended_at", None, ended_at, reason, correction_request)
    return session


def void_session(session: WorkSession, actor: User, reason: str | None = None) -> None:
    if session.voided_at is not None:
        raise TimeTrackingError("Die Buchung ist bereits storniert.")
    session.voided_at = utcnow_naive()
    session.voided_by_user_id = actor.id
    session.open_marker = None
    for work_break in session.breaks:
        work_break.open_marker = None
    _record(actor, session, "session", "void", reason=reason)


def _validate_break(session: WorkSession, started_at: datetime, ended_at: datetime | None, exclude_id=None) -> None:
    now = utcnow_naive()
    if session.voided_at is not None:
        raise TimeTrackingError("Die zugehörige Buchung ist storniert.")
    if ended_at is None:
        raise TimeTrackingError("Bitte ein Pausenende angeben.")
    _validate_range(started_at, ended_at, now)
    session_stop = session.ended_at or now
    if started_at < session.started_at or ended_at > session_stop:
        raise TimeTrackingError("Die Pause muss innerhalb der Buchung liegen.")
    for other in session.active_breaks:
        if other.id == exclude_id:
            continue
        other_end = other.ended_at or now
        if other.started_at < ended_at and other_end > started_at:
            raise TimeTrackingError("Die Pause überschneidet sich mit einer anderen Pause.")


def update_break_times(
    work_break: WorkBreak, started_at: datetime, ended_at: datetime | None, actor: User, reason: str | None = None
) -> bool:
    if work_break.voided_at is not None:
        raise TimeTrackingError("Stornierte Pausen können nicht geändert werden.")
    _validate_break(work_break.work_session, started_at, ended_at, exclude_id=work_break.id)
    changed = False
    if started_at != work_break.started_at:
        _record(actor, work_break, "break", "update", "started_at", work_break.started_at, started_at, reason)
        work_break.started_at = started_at
        changed = True
    if ended_at != work_break.ended_at:
        _record(actor, work_break, "break", "update", "ended_at", work_break.ended_at, ended_at, reason)
        work_break.ended_at = ended_at
        changed = True
    work_break.open_marker = None
    if changed:
        work_break.is_corrected = True
    return changed


def add_break(session: WorkSession, started_at: datetime, ended_at: datetime, actor: User, reason: str | None = None) -> WorkBreak:
    _validate_break(session, started_at, ended_at)
    work_break = WorkBreak(
        tenant_id=session.tenant_id,
        work_session_id=session.id,
        user_id=session.user_id,
        started_at=started_at,
        ended_at=ended_at,
        is_corrected=True,
    )
    db.session.add(work_break)
    db.session.flush()
    _record(actor, work_break, "break", "create", "started_at", None, started_at, reason)
    _record(actor, work_break, "break", "create", "ended_at", None, ended_at, reason)
    return work_break


def void_break(work_break: WorkBreak, actor: User, reason: str | None = None) -> None:
    if work_break.voided_at is not None:
        raise TimeTrackingError("Die Pause ist bereits storniert.")
    work_break.voided_at = utcnow_naive()
    work_break.voided_by_user_id = actor.id
    work_break.open_marker = None
    _record(actor, work_break, "break", "void", reason=reason)


# --- Korrekturantraege ---------------------------------------------------------------------


def create_correction_request(
    user: User,
    session: WorkSession | None,
    requested_started_at: datetime,
    requested_ended_at: datetime,
    reason: str,
) -> TimeCorrectionRequest:
    reason = (reason or "").strip()
    if not reason:
        raise TimeTrackingError("Bitte begründen Sie die gewünschte Korrektur.")
    _validate_range(requested_started_at, requested_ended_at, utcnow_naive())
    if session is not None:
        if session.user_id != user.id:
            raise TimeTrackingError("Diese Buchung gehört nicht zu Ihrem Konto.")
        if session.voided_at is not None:
            raise TimeTrackingError("Für stornierte Buchungen sind keine Anträge möglich.")
        pending = TimeCorrectionRequest.query.filter_by(
            work_session_id=session.id, status=CorrectionRequestStatus.PENDING
        ).first()
        if pending is not None:
            raise TimeTrackingError("Für diese Buchung liegt bereits ein offener Antrag vor.")
    correction_request = TimeCorrectionRequest(
        tenant_id=user.tenant_id,
        user_id=user.id,
        work_session_id=session.id if session is not None else None,
        work_date=local_date_of(requested_started_at),
        original_started_at=session.started_at if session is not None else None,
        original_ended_at=session.ended_at if session is not None else None,
        requested_started_at=requested_started_at,
        requested_ended_at=requested_ended_at,
        reason=reason,
    )
    db.session.add(correction_request)
    return correction_request


def _require_pending(correction_request: TimeCorrectionRequest) -> None:
    if correction_request.status != CorrectionRequestStatus.PENDING:
        raise TimeTrackingError("Dieser Antrag wurde bereits entschieden.")


def approve_correction_request(correction_request: TimeCorrectionRequest, actor: User, note: str | None = None) -> None:
    _require_pending(correction_request)
    note = (note or "").strip() or None
    reason = f"Antrag #{correction_request.id}: {correction_request.reason}"
    if correction_request.work_session is not None:
        update_session_times(
            correction_request.work_session,
            correction_request.requested_started_at,
            correction_request.requested_ended_at,
            actor,
            reason,
            correction_request,
        )
    else:
        create_manual_session(
            correction_request.user,
            correction_request.requested_started_at,
            correction_request.requested_ended_at,
            actor,
            reason,
            correction_request,
        )
    correction_request.status = CorrectionRequestStatus.APPROVED
    correction_request.decided_by_user_id = actor.id
    correction_request.decided_at = utcnow_naive()
    correction_request.decision_note = note


def reject_correction_request(correction_request: TimeCorrectionRequest, actor: User, note: str | None = None) -> None:
    _require_pending(correction_request)
    correction_request.status = CorrectionRequestStatus.REJECTED
    correction_request.decided_by_user_id = actor.id
    correction_request.decided_at = utcnow_naive()
    correction_request.decision_note = (note or "").strip() or None


def ensure_profile(user: User) -> EmployeeProfile:
    profile = user.employee_profile
    if profile is None:
        profile = EmployeeProfile(tenant_id=user.tenant_id, user=user)
        db.session.add(profile)
    return profile
