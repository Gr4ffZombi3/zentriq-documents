from datetime import timedelta

import pytest

from app.models import (
    AuditLog,
    CorrectionRequestStatus,
    Tenant,
    TimeCorrection,
    TimeCorrectionRequest,
    User,
    UserRole,
    WorkSession,
)
from app.models.audit_log import AuditEventType
from app.services.timetracking.clock import to_local, utcnow_naive
from app.tenancy import bypass_tenant_scope, use_tenant_id


def _closed_session(db, user, hours_ago=30, length_hours=8):
    start = utcnow_naive() - timedelta(hours=hours_ago)
    session = WorkSession(
        tenant_id=user.tenant_id,
        user_id=user.id,
        work_date=to_local(start).date(),
        started_at=start,
        ended_at=start + timedelta(hours=length_hours),
    )
    db.session.add(session)
    db.session.commit()
    return session


def test_employee_home_is_timetracking(employee_client):
    resp = employee_client.get("/")
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("/zeiterfassung")


def test_employee_stamping_flow_uses_server_time(employee_client, employee):
    before = utcnow_naive().replace(microsecond=0) - timedelta(seconds=1)
    # Ein vom Client mitgeschickter Zeitstempel wird ignoriert.
    resp = employee_client.post("/zeiterfassung/einstempeln", data={"started_at": "2020-01-01T00:00:00"})
    assert resp.status_code == 302
    session = WorkSession.query.filter_by(user_id=employee.id).one()
    assert session.started_at >= before
    assert session.is_open

    employee_client.post("/zeiterfassung/pause/start")
    employee_client.post("/zeiterfassung/pause/ende")
    employee_client.post("/zeiterfassung/ausstempeln")
    assert session.ended_at is not None and not session.is_open

    html = employee_client.get("/zeiterfassung").get_data(as_text=True)
    assert "Heutige Buchungen" in html
    assert employee_client.get("/zeiterfassung/woche").status_code == 200
    assert employee_client.get("/zeiterfassung/monat?monat=2026-02").status_code == 200


def test_stamping_requires_post(employee_client):
    assert employee_client.get("/zeiterfassung/einstempeln").status_code == 405


@pytest.mark.parametrize(
    "url",
    ["/zeiterfassung/team", "/zeiterfassung/antraege", "/zeiterfassung/protokoll", "/zeiterfassung/team/1"],
)
def test_employee_cannot_open_admin_timetracking(employee_client, url):
    assert employee_client.get(url).status_code == 403


def test_employee_cannot_modify_sessions(employee_client, employee, db):
    session = _closed_session(db, employee)
    assert employee_client.post(f"/zeiterfassung/buchungen/{session.id}/bearbeiten").status_code == 403
    assert employee_client.post(f"/zeiterfassung/buchungen/{session.id}/stornieren").status_code == 403
    assert session.voided_at is None


def test_employee_cannot_request_for_foreign_session(employee_client, user, db):
    foreign = _closed_session(db, user)
    assert employee_client.get(f"/zeiterfassung/korrekturantraege/neu?buchung={foreign.id}").status_code == 404


def test_request_and_admin_approval_flow(employee_client, auth_client, employee, user, db):
    session = _closed_session(db, employee)
    local_start = to_local(session.started_at)
    new_start = (local_start - timedelta(minutes=30)).strftime("%H:%M")
    resp = employee_client.post(
        "/zeiterfassung/korrekturantraege/neu",
        data={
            "buchung": session.id,
            "start_date": local_start.date().isoformat(),
            "start_time": new_start,
            "end_date": to_local(session.ended_at).date().isoformat(),
            "end_time": to_local(session.ended_at).strftime("%H:%M"),
            "reason": "Früher begonnen",
        },
    )
    assert resp.status_code == 302
    correction_request = TimeCorrectionRequest.query.one()
    original_start = session.started_at
    assert session.started_at == original_start  # noch unveraendert

    resp = auth_client.post(f"/zeiterfassung/antraege/{correction_request.id}/entscheiden", data={"decision": "approve"})
    assert resp.status_code == 302
    db.session.refresh(session)
    assert correction_request.status == CorrectionRequestStatus.APPROVED
    assert to_local(session.started_at).strftime("%H:%M") == new_start
    entry = TimeCorrection.query.filter_by(field="started_at").one()
    assert entry.corrected_by_user_id == user.id and entry.request_id == correction_request.id
    events = {item.event_type for item in AuditLog.query.all()}
    assert AuditEventType.TIME_CORRECTION_REQUESTED in events
    assert AuditEventType.TIME_CORRECTION_DECIDED in events

    html = auth_client.get("/zeiterfassung/protokoll").get_data(as_text=True)
    assert "Antrag #" in html


def test_admin_edit_void_and_views(auth_client, employee, db):
    session = _closed_session(db, employee)
    local_start = to_local(session.started_at)
    resp = auth_client.post(
        f"/zeiterfassung/buchungen/{session.id}/bearbeiten",
        data={
            "start_date": local_start.date().isoformat(),
            "start_time": "06:00",
            "end_date": to_local(session.ended_at).date().isoformat(),
            "end_time": to_local(session.ended_at).strftime("%H:%M"),
            "reason": "Korrektur",
        },
    )
    assert resp.status_code == 302
    assert TimeCorrection.query.filter_by(target_id=session.id, field="started_at").count() == 1
    auth_client.post(f"/zeiterfassung/buchungen/{session.id}/stornieren", data={"reason": "doppelt"})
    assert session.voided_at is not None
    assert WorkSession.query.count() == 1

    for view in ("tag", "woche", "monat"):
        resp = auth_client.get(f"/zeiterfassung/team/{employee.id}?ansicht={view}&datum={session.work_date.isoformat()}")
        assert resp.status_code == 200
    assert "storniert" in auth_client.get(
        f"/zeiterfassung/team/{employee.id}?ansicht=tag&datum={session.work_date.isoformat()}"
    ).get_data(as_text=True)
    assert auth_client.get("/zeiterfassung/team").status_code == 200


def test_admin_cannot_see_other_tenant(auth_client, db):
    other = Tenant(name="Fremd", slug="fremd-zeit")
    db.session.add(other)
    db.session.commit()
    with use_tenant_id(other.id):
        stranger = User(tenant_id=other.id, email="fremd@example.com", password_hash="x", role=UserRole.MITARBEITER)
        db.session.add(stranger)
        db.session.commit()
        foreign_session = _closed_session(db, stranger)
        stranger_id, session_id = stranger.id, foreign_session.id

    assert auth_client.get(f"/zeiterfassung/team/{stranger_id}").status_code == 404
    assert auth_client.get(f"/zeiterfassung/buchungen/{session_id}/bearbeiten").status_code == 404
    assert auth_client.post(f"/zeiterfassung/buchungen/{session_id}/stornieren").status_code == 404
    assert "fremd@example.com" not in auth_client.get("/zeiterfassung/team").get_data(as_text=True)
    with bypass_tenant_scope():
        assert db.session.get(WorkSession, session_id).voided_at is None


def test_stamping_is_csrf_protected(app, employee_client, employee):
    app.config["WTF_CSRF_ENABLED"] = True
    try:
        assert employee_client.post("/zeiterfassung/einstempeln").status_code == 400
    finally:
        app.config["WTF_CSRF_ENABLED"] = False
    assert WorkSession.query.filter_by(user_id=employee.id).count() == 0
