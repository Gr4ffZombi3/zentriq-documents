"""Buero-Admin beendet bzw. korrigiert Zeitbuchungen von Mitarbeitern (vergessenes Ausstempeln,
vergessenes Einstempeln), inkl. Protokoll, Summen/Export, Rechten, Doppelaktionen und
Zeitumstellung."""

from datetime import date, time, timedelta

import pytest

from app.models import Tenant, TimeCorrection, User, UserRole, WorkBreak, WorkSession
from app.services.timetracking import service
from app.services.timetracking.calc import month_bounds
from app.services.timetracking.clock import local_to_utc_naive, to_local, utcnow_naive
from app.services.timetracking.service import TimeTrackingError
from app.tenancy import bypass_tenant_scope, use_tenant_id


def _local_parts(value):
    local = to_local(value)
    return local.date().isoformat(), local.strftime("%H:%M")


def _open_session(db, user, hours_ago=3):
    start = (utcnow_naive() - timedelta(hours=hours_ago)).replace(second=0, microsecond=0)
    session = WorkSession(tenant_id=user.tenant_id, user_id=user.id, work_date=to_local(start).date(), started_at=start, open_marker=1)
    db.session.add(session)
    db.session.commit()
    return session


# 1. Laura stempelt sich ein; der Buero-Admin sieht sie und stempelt sie aus. ------------------

def test_admin_sees_running_session_and_clocks_out_now(employee_client, auth_client, employee, db):
    employee_client.post("/zeiterfassung/einstempeln")
    session = WorkSession.query.filter_by(user_id=employee.id).one()

    team = auth_client.get("/zeiterfassung/team").get_data(as_text=True)
    assert "Aktuell eingestempelt" in team
    assert f"/zeiterfassung/buchungen/{session.id}/ausstempeln" in team

    resp = auth_client.post(
        f"/zeiterfassung/buchungen/{session.id}/ausstempeln",
        data={"zeitpunkt": "jetzt", "reason": "Ausstempeln vergessen", "next": "/zeiterfassung/team"},
    )
    assert resp.status_code == 302 and resp.headers["Location"].endswith("/zeiterfassung/team")
    db.session.refresh(session)
    assert session.ended_at is not None and not session.is_open and session.is_corrected
    assert "Aktuell eingestempelt" not in auth_client.get("/zeiterfassung/team").get_data(as_text=True)
    # Laura ist danach ausgestempelt und kann sich normal wieder einstempeln.
    assert service.get_stamp_state(employee.id).status == "out"
    employee_client.post("/zeiterfassung/einstempeln")
    assert WorkSession.query.filter_by(user_id=employee.id, open_marker=1).count() == 1


def test_admin_clock_out_also_ends_running_break(employee_client, auth_client, employee, db):
    employee_client.post("/zeiterfassung/einstempeln")
    employee_client.post("/zeiterfassung/pause/start")
    session = WorkSession.query.filter_by(user_id=employee.id).one()
    auth_client.post(f"/zeiterfassung/buchungen/{session.id}/ausstempeln", data={"zeitpunkt": "jetzt", "reason": "Pause nicht beendet"})
    work_break = WorkBreak.query.filter_by(work_session_id=session.id).one()
    db.session.refresh(session)
    assert session.ended_at is not None
    assert work_break.ended_at == session.ended_at and work_break.open_marker is None


# 2. Vergessenes Arbeitsende nachtragen (Vergangenheit, auch nach Tageswechsel) ----------------

def test_admin_enters_past_end_and_change_is_logged(auth_client, user, employee, db):
    session = _open_session(db, employee, hours_ago=20)  # laeuft seit gestern
    team = auth_client.get("/zeiterfassung/team").get_data(as_text=True)
    assert "möglicherweise vergessen" in team

    end = session.started_at + timedelta(hours=8, minutes=30)
    end_date, end_time = _local_parts(end)
    auth_client.post(
        f"/zeiterfassung/buchungen/{session.id}/ausstempeln",
        data={"zeitpunkt": "zeit", "end_date": end_date, "end_time": end_time, "reason": "Laut Laura um Feierabend gegangen"},
    )
    db.session.refresh(session)
    assert session.ended_at == end
    # 5. Protokoll: alter/neuer Wert, Akteur, Zeitpunkt, Begruendung
    entry = TimeCorrection.query.filter_by(target_type="session", target_id=session.id, field="ended_at").one()
    assert entry.old_value is None and entry.new_value.startswith(end.strftime("%Y-%m-%dT%H:%M"))
    assert entry.corrected_by_user_id == user.id and entry.reason == "Laut Laura um Feierabend gegangen"
    assert entry.created_at is not None
    assert "Laut Laura" in auth_client.get("/zeiterfassung/protokoll").get_data(as_text=True)


def test_released_stale_session_can_be_closed(employee_client, auth_client, employee, db):
    old = _open_session(db, employee, hours_ago=30)
    employee_client.post("/zeiterfassung/einstempeln")  # loest die alte Buchung ("Ausstempeln fehlt")
    db.session.refresh(old)
    assert old.is_incomplete
    assert "Ausstempeln fehlt" in auth_client.get("/zeiterfassung/team").get_data(as_text=True)
    end_date, end_time = _local_parts(old.started_at + timedelta(hours=8))
    auth_client.post(
        f"/zeiterfassung/buchungen/{old.id}/ausstempeln",
        data={"zeitpunkt": "zeit", "end_date": end_date, "end_time": end_time, "reason": "Nachgetragen"},
    )
    db.session.refresh(old)
    assert old.ended_at == old.started_at + timedelta(hours=8)


@pytest.mark.parametrize(
    "offset, message",
    [(timedelta(hours=-1), "nach dem Beginn"), (timedelta(hours=5), "Zukunft")],
)
def test_invalid_end_is_rejected(auth_client, employee, db, offset, message):
    session = _open_session(db, employee, hours_ago=3)
    end_date, end_time = _local_parts(session.started_at + offset)
    resp = auth_client.post(
        f"/zeiterfassung/buchungen/{session.id}/ausstempeln",
        data={"zeitpunkt": "zeit", "end_date": end_date, "end_time": end_time, "reason": "Test"},
        follow_redirects=True,
    )
    assert message in resp.get_data(as_text=True)
    db.session.refresh(session)
    assert session.ended_at is None and session.is_open


def test_reason_is_required(auth_client, employee, db):
    session = _open_session(db, employee)
    resp = auth_client.post(f"/zeiterfassung/buchungen/{session.id}/ausstempeln", data={"zeitpunkt": "jetzt", "reason": " "}, follow_redirects=True)
    assert "Begründung" in resp.get_data(as_text=True)
    db.session.refresh(session)
    assert session.is_open


# 3. Fehlende vergangene Arbeitszeit (Einstempeln vergessen) nachtragen -----------------------

def test_admin_adds_missing_past_session_without_overlap(auth_client, employee, db):
    day = (to_local(utcnow_naive()) - timedelta(days=2)).date()
    data = {"start_date": day.isoformat(), "start_time": "08:00", "end_date": day.isoformat(), "end_time": "12:00", "reason": "Einstempeln vergessen"}
    assert auth_client.post(f"/zeiterfassung/team/{employee.id}/buchungen/neu", data=data).status_code == 302
    assert WorkSession.query.filter_by(user_id=employee.id).count() == 1
    # Ueberschneidung wird abgelehnt, nichts doppelt angelegt.
    data.update(start_time="11:00", end_time="13:00")
    html = auth_client.post(f"/zeiterfassung/team/{employee.id}/buchungen/neu", data=data, follow_redirects=True).get_data(as_text=True)
    assert "überschneiden" in html
    assert WorkSession.query.filter_by(user_id=employee.id).count() == 1
    # Eingaben bleiben nach dem Fehler erhalten.
    assert 'value="11:00"' in html and "Einstempeln vergessen" in html


# 4. Summen und Export beruecksichtigen die Korrektur -----------------------------------------

def test_summary_and_export_include_correction(auth_client, employee, db):
    session = _open_session(db, employee, hours_ago=20)
    end = session.started_at + timedelta(hours=7, minutes=15)
    end_date, end_time = _local_parts(end)
    auth_client.post(
        f"/zeiterfassung/buchungen/{session.id}/ausstempeln",
        data={"zeitpunkt": "zeit", "end_date": end_date, "end_time": end_time, "reason": "Korrektur"},
    )
    start, stop = month_bounds(session.work_date)
    summary = service.summarize_user_period(employee, start, stop)
    assert summary.net_seconds == 7 * 3600 + 15 * 60
    csv = auth_client.get(f"/zeiterfassung/team/{employee.id}/export.csv?monat={session.work_date.strftime('%Y-%m')}").get_data(as_text=True)
    assert "7:15" in csv
    team_csv = auth_client.get(f"/zeiterfassung/team/export.csv?monat={session.work_date.strftime('%Y-%m')}").get_data(as_text=True)
    assert "7:15" in team_csv


# 6. Gleichzeitiges/doppeltes Ausstempeln -----------------------------------------------------

def test_double_clock_out_keeps_single_consistent_end(employee_client, auth_client, employee, db):
    employee_client.post("/zeiterfassung/einstempeln")
    session = WorkSession.query.filter_by(user_id=employee.id).one()
    # Laura stempelt selbst aus, der Admin hatte die Seite noch offen und klickt danach.
    employee_client.post("/zeiterfassung/ausstempeln")
    db.session.refresh(session)
    first_end = session.ended_at
    html = auth_client.post(
        f"/zeiterfassung/buchungen/{session.id}/ausstempeln", data={"zeitpunkt": "jetzt", "reason": "doppelt"}, follow_redirects=True
    ).get_data(as_text=True)
    assert "bereits beendet" in html
    db.session.refresh(session)
    assert session.ended_at == first_end
    assert TimeCorrection.query.filter_by(target_id=session.id, field="ended_at").count() == 0
    # Zweiter Admin-Klick auf eine schon beendete Buchung aendert ebenfalls nichts.
    auth_client.post(f"/zeiterfassung/buchungen/{session.id}/ausstempeln", data={"zeitpunkt": "jetzt", "reason": "nochmal"})
    assert WorkSession.query.filter_by(user_id=employee.id).count() == 1


def test_employee_clock_out_after_admin_end_is_rejected(employee_client, auth_client, employee, db):
    employee_client.post("/zeiterfassung/einstempeln")
    session = WorkSession.query.filter_by(user_id=employee.id).one()
    auth_client.post(f"/zeiterfassung/buchungen/{session.id}/ausstempeln", data={"zeitpunkt": "jetzt", "reason": "vom Admin"})
    db.session.refresh(session)
    end = session.ended_at
    resp = employee_client.post("/zeiterfassung/ausstempeln", headers={"Accept": "application/json"})
    assert resp.status_code in (302, 409, 400)
    db.session.refresh(session)
    assert session.ended_at == end


# 7. Rechte: Mitarbeiter und fremde Buero-Admins ----------------------------------------------

def test_employee_cannot_end_sessions(employee_client, user, employee, db):
    own = _open_session(db, employee)
    colleague = _open_session(db, user)
    for target in (own, colleague):
        assert employee_client.post(f"/zeiterfassung/buchungen/{target.id}/ausstempeln", data={"zeitpunkt": "jetzt", "reason": "x" * 5}).status_code == 403
        db.session.refresh(target)
        assert target.is_open


def test_foreign_office_admin_cannot_end_session(app, employee, db):
    session = _open_session(db, employee)
    other = Tenant(name="Fremdbüro", slug="fremdbuero")
    db.session.add(other)
    db.session.commit()
    with use_tenant_id(other.id):
        stranger = User(tenant_id=other.id, email="admin-fremd@example.com", is_active=True, role=UserRole.OFFICE_ADMIN)
        stranger.set_password("fremdpasswort123")
        db.session.add(stranger)
        db.session.commit()
    client = app.test_client()
    client.post("/auth/login", data={"login_type": "email", "identifier": "admin-fremd@example.com", "password": "fremdpasswort123"})
    assert client.post(f"/zeiterfassung/buchungen/{session.id}/ausstempeln", data={"zeitpunkt": "jetzt", "reason": "fremd"}).status_code == 404
    assert "Aktuell eingestempelt" not in client.get("/zeiterfassung/team").get_data(as_text=True)
    with bypass_tenant_scope():
        assert db.session.get(WorkSession, session.id).is_open


# Zeitumstellung und Tageswechsel -------------------------------------------------------------

def test_nonexistent_local_time_on_dst_switch_is_rejected(auth_client, employee, db):
    data = {"start_date": "2026-03-29", "start_time": "02:30", "end_date": "2026-03-29", "end_time": "05:00", "reason": "Test"}
    html = auth_client.post(f"/zeiterfassung/team/{employee.id}/buchungen/neu", data=data, follow_redirects=True).get_data(as_text=True)
    assert "Zeitumstellung" in html
    assert WorkSession.query.count() == 0


@pytest.mark.parametrize(
    "day, start, end, expected_hours",
    [
        (date(2026, 3, 29), time(1, 0), time(4, 0), 2),  # Sommerzeit: 1 Stunde faellt weg
        (date(2026, 10, 25), time(1, 0), time(4, 0), 4),  # Winterzeit: 1 Stunde doppelt
    ],
)
def test_durations_across_dst(employee, user, db, day, start, end, expected_hours):
    started = local_to_utc_naive(day, start)
    ended = local_to_utc_naive(day, end)
    service.create_manual_session(employee, started, ended, user, "DST-Test")
    db.session.commit()
    summary = service.summarize_user_period(employee, day, day)
    assert summary.net_seconds == expected_hours * 3600


def test_session_across_midnight_counts_for_start_day(employee, user, db):
    day = date(2026, 1, 14)
    started = local_to_utc_naive(day, time(22, 0))
    ended = local_to_utc_naive(day + timedelta(days=1), time(2, 0))
    session = service.create_manual_session(employee, started, ended, user, "Nachtschicht")
    db.session.commit()
    assert session.work_date == day
    assert service.summarize_user_period(employee, day, day + timedelta(days=1)).net_seconds == 4 * 3600


def test_admin_end_session_service_rejects_second_call(employee, user, db):
    session = _open_session(db, employee)
    end = utcnow_naive().replace(microsecond=0)
    service.admin_end_session(session, end, user, "erste Aktion")
    db.session.commit()
    with pytest.raises(TimeTrackingError):
        service.admin_end_session(session, end + timedelta(minutes=1), user, "zweite Aktion")
    db.session.rollback()
    assert db.session.get(WorkSession, session.id).ended_at == end
