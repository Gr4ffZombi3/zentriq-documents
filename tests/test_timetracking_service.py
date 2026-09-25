from datetime import date, datetime, timedelta

import pytest

from app.models import (
    CorrectionRequestStatus,
    EmployeeProfile,
    Tenant,
    TimeCorrection,
    User,
    UserRole,
    WorkBreak,
    WorkSession,
)
from app.services.timetracking import service
from app.services.timetracking.calc import format_duration, summarize_period, target_seconds_for_day
from app.services.timetracking.clock import local_to_utc_naive
from app.services.timetracking.service import TimeTrackingError
from app.tenancy import use_tenant_id


class Clock:
    def __init__(self, start):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, **kwargs):
        self.now += timedelta(**kwargs)


@pytest.fixture()
def clock(monkeypatch):
    fake = Clock(datetime(2026, 9, 21, 6, 0))  # Montag, 08:00 Berliner Zeit
    monkeypatch.setattr(service, "utcnow_naive", fake)
    return fake


@pytest.fixture()
def worker(db, tenant):
    worker_user = User(tenant_id=tenant.id, email="ma@example.com", password_hash="x", role=UserRole.MITARBEITER)
    db.session.add(worker_user)
    db.session.flush()
    db.session.add(EmployeeProfile(tenant_id=tenant.id, user_id=worker_user.id, weekly_target_minutes=2400))
    db.session.commit()
    return worker_user


def test_full_day_uses_server_time(clock, worker):
    session = service.clock_in(worker)
    assert session.started_at == datetime(2026, 9, 21, 6, 0)
    assert session.work_date == date(2026, 9, 21)
    clock.advance(hours=4)
    service.start_break(worker)
    clock.advance(minutes=30)
    service.end_break(worker)
    clock.advance(hours=4)
    service.clock_out(worker)

    summary = service.summarize_user_period(worker, date(2026, 9, 21), date(2026, 9, 21), now=clock.now)
    day = summary.days[0]
    assert day.net_seconds == 8 * 3600
    assert day.break_seconds == 30 * 60
    assert day.target_seconds == 8 * 3600
    assert summary.balance_seconds == 0


def test_double_clock_in_is_rejected(clock, worker):
    service.clock_in(worker)
    with pytest.raises(TimeTrackingError):
        service.clock_in(worker)
    assert WorkSession.query.count() == 1


def test_break_and_clock_out_require_session(clock, worker):
    with pytest.raises(TimeTrackingError):
        service.start_break(worker)
    with pytest.raises(TimeTrackingError):
        service.clock_out(worker)
    service.clock_in(worker)
    service.start_break(worker)
    with pytest.raises(TimeTrackingError):
        service.start_break(worker)


def test_clock_out_closes_running_break(clock, worker):
    service.clock_in(worker)
    clock.advance(hours=1)
    service.start_break(worker)
    clock.advance(minutes=10)
    service.clock_out(worker)
    work_break = WorkBreak.query.one()
    assert work_break.ended_at == clock.now
    assert work_break.open_marker is None


def test_stale_session_is_released_without_invented_end(clock, worker):
    stale = service.clock_in(worker)
    clock.advance(days=1)
    state = service.get_stamp_state(worker.id, clock.now)
    assert state.stale and state.status == "out"
    with pytest.raises(TimeTrackingError):
        service.clock_out(worker)

    service.clock_in(worker)
    assert stale.ended_at is None
    assert stale.is_incomplete
    assert TimeCorrection.query.filter_by(target_id=stale.id, action="release").count() == 1


def test_admin_correction_keeps_old_value(clock, worker, user, db):
    session = service.clock_in(worker)
    clock.advance(hours=8)
    service.clock_out(worker)
    old_start = session.started_at
    new_start = old_start - timedelta(minutes=30)

    changed = service.update_session_times(session, new_start, session.ended_at, user, "Vergessen einzustempeln")
    db.session.commit()

    assert changed and session.is_corrected
    entry = TimeCorrection.query.filter_by(field="started_at").one()
    assert entry.old_value == old_start.strftime("%Y-%m-%dT%H:%M:%SZ")
    assert entry.new_value == new_start.strftime("%Y-%m-%dT%H:%M:%SZ")
    assert entry.corrected_by_user_id == user.id
    assert entry.corrected_by_email_snapshot == user.email
    assert entry.reason == "Vergessen einzustempeln"


def test_correction_rejects_future_and_overlap(clock, worker, user):
    first = service.clock_in(worker)
    clock.advance(hours=2)
    service.clock_out(worker)
    clock.advance(hours=1)
    second = service.clock_in(worker)
    clock.advance(hours=2)
    service.clock_out(worker)

    with pytest.raises(TimeTrackingError):
        service.update_session_times(second, first.started_at + timedelta(minutes=30), second.ended_at, user)
    with pytest.raises(TimeTrackingError):
        service.update_session_times(second, second.started_at, clock.now + timedelta(hours=1), user)


def test_void_instead_of_delete(clock, worker, user, db):
    session = service.clock_in(worker)
    clock.advance(hours=1)
    service.clock_out(worker)
    service.void_session(session, user, "Doppelt erfasst")
    db.session.commit()
    assert WorkSession.query.count() == 1
    assert session.voided_at is not None
    summary = service.summarize_user_period(worker, session.work_date, session.work_date, now=clock.now)
    assert summary.net_seconds == 0


def test_correction_request_approve_applies_and_logs(clock, worker, user, db):
    session = service.clock_in(worker)
    clock.advance(hours=8)
    service.clock_out(worker)
    requested_start = session.started_at - timedelta(minutes=15)
    correction_request = service.create_correction_request(
        worker, session, requested_start, session.ended_at, "Terminbeginn vor Stempeln"
    )
    db.session.commit()
    assert session.started_at != requested_start  # Antrag aendert noch nichts

    service.approve_correction_request(correction_request, user, "ok")
    db.session.commit()
    assert session.started_at == requested_start
    assert correction_request.status == CorrectionRequestStatus.APPROVED
    assert correction_request.decided_by_user_id == user.id
    assert TimeCorrection.query.filter_by(request_id=correction_request.id).count() >= 1
    with pytest.raises(TimeTrackingError):
        service.reject_correction_request(correction_request, user)


def test_correction_request_for_missing_day_creates_manual_session(clock, worker, user, db):
    clock.advance(days=2)
    start = local_to_utc_naive(date(2026, 9, 21), datetime.strptime("08:00", "%H:%M").time())
    end = start + timedelta(hours=6)
    correction_request = service.create_correction_request(worker, None, start, end, "Außentermin")
    db.session.commit()
    service.approve_correction_request(correction_request, user)
    db.session.commit()
    created = WorkSession.query.one()
    assert created.source.value == "manual"
    assert created.work_date == date(2026, 9, 21)


def test_employee_cannot_request_for_foreign_session(clock, worker, user, db):
    session = service.clock_in(user)
    clock.advance(hours=1)
    service.clock_out(user)
    with pytest.raises(TimeTrackingError):
        service.create_correction_request(worker, session, session.started_at, session.ended_at, "x")


def test_request_requires_reason(clock, worker):
    with pytest.raises(TimeTrackingError):
        service.create_correction_request(worker, None, clock.now - timedelta(hours=2), clock.now, "  ")


def test_sessions_are_tenant_isolated(clock, worker, db):
    other = Tenant(name="Fremd", slug="fremd")
    db.session.add(other)
    db.session.commit()
    service.clock_in(worker)
    with use_tenant_id(other.id):
        assert WorkSession.query.count() == 0


def test_target_and_formatting():
    profile = EmployeeProfile(weekly_target_minutes=2400, workdays="12345")
    assert target_seconds_for_day(profile, date(2026, 9, 21)) == 8 * 3600
    assert target_seconds_for_day(profile, date(2026, 9, 26)) == 0  # Samstag
    assert format_duration(8 * 3600 + 5 * 60) == "8:05"
    assert format_duration(-90 * 60, signed=True) == "−1:30"
    assert format_duration(30 * 60, signed=True) == "+0:30"


def test_target_only_counts_until_today():
    profile = EmployeeProfile(weekly_target_minutes=2400, workdays="12345")
    summary = summarize_period([], date(2026, 9, 21), date(2026, 9, 27), profile, date(2026, 9, 22), datetime(2026, 9, 22))
    assert summary.target_seconds == 16 * 3600
    assert summary.balance_seconds == -16 * 3600


def test_local_time_conversion_handles_dst():
    summer = local_to_utc_naive(date(2026, 7, 1), datetime.strptime("08:00", "%H:%M").time())
    winter = local_to_utc_naive(date(2026, 12, 1), datetime.strptime("08:00", "%H:%M").time())
    assert summer.hour == 6
    assert winter.hour == 7
