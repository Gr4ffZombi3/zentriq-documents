# ruff: noqa: F811  (Fixtures werden aus anderen Testmodulen importiert)
"""Krankheit, Urlaub und Feiertage in der Zeiterfassung. Fiktive Daten.

Laura: Teilzeit Mo/Di/Do je 4 Std. (Wochensoll 12 Std.), Dennis: Mo-Fr je 8 Std. Buero A in NRW.
Alle Daten liegen in der Vergangenheit, damit das 'Soll bis heute' sie vollstaendig zaehlt."""

import re
from datetime import date, datetime

import pytest

from app.extensions import db as _db
from app.models import EmployeeProfile, TimeAbsence, TimeCorrection, User
from app.services.timetracking.calc import summarize_period
from app.services.timetracking.clock import local_to_utc_naive
from app.services.timetracking.holidays import easter_sunday, holidays_for
from app.tenancy import bypass_tenant_scope
from tests.test_permissions_security import login, world  # noqa: F401

H = 3600


@pytest.fixture()
def staff(app, world):
    with bypass_tenant_scope():
        for user_id, minutes, days in ((world.laura_id, 720, "124"), (world.dennis_id, 2400, "12345")):
            profile = EmployeeProfile.query.filter_by(user_id=user_id).first()
            if profile is None:
                profile = EmployeeProfile(tenant_id=world.tenant_a.id, user_id=user_id)
                _db.session.add(profile)
            profile.weekly_target_minutes, profile.workdays = minutes, days
        _db.session.commit()
    return world


def _add(client, user_id, start, end=None, kind="sick", follow=False):
    return client.post("/zeiterfassung/abwesenheiten/neu", follow_redirects=follow,
                       data={"user_id": str(user_id), "kind": kind, "start_date": start, "end_date": end or start})


def _month(client, user_id, month):
    return client.get(f"/zeiterfassung/team/{user_id}?ansicht=monat&datum={month}-01").get_data(as_text=True)


def _summary(user_id, start, end):
    from app.services.timetracking import service

    with bypass_tenant_scope():
        _db.session.expire_all()
        return service.summarize_user_period(_db.session.get(User, user_id), start, end)


def _absence(**filters):
    with bypass_tenant_scope():
        _db.session.expire_all()
        return TimeAbsence.query.filter_by(**filters).order_by(TimeAbsence.id.desc()).first()


def _work(user_id, day, start, end, actor_id):
    from app.services.timetracking import service

    with bypass_tenant_scope():
        member, actor = _db.session.get(User, user_id), _db.session.get(User, actor_id)
        service.create_manual_session(member, local_to_utc_naive(day, datetime.strptime(start, "%H:%M").time()),
                                      local_to_utc_naive(day, datetime.strptime(end, "%H:%M").time()), actor, "nachgetragen")
        _db.session.commit()


# --- Feiertage ------------------------------------------------------------------------------------


def test_holidays_nrw_and_other_states():
    assert easter_sunday(2026) == date(2026, 4, 5) and easter_sunday(2027) == date(2027, 3, 28)
    nrw = holidays_for(2026, "NW")
    assert nrw[date(2026, 6, 4)] == "Fronleichnam" and nrw[date(2026, 11, 1)] == "Allerheiligen"
    assert nrw[date(2026, 4, 3)] == "Karfreitag" and nrw[date(2026, 5, 14)] == "Christi Himmelfahrt"
    assert date(2026, 10, 31) not in nrw and date(2026, 1, 6) not in nrw
    assert date(2026, 10, 31) in holidays_for(2026, "TH") and date(2026, 9, 20) in holidays_for(2026, "TH")
    assert holidays_for(2026, "SN")[date(2026, 11, 18)] == "Buß- und Bettag"
    assert len(holidays_for(2026, "HH")) == 10


def test_holiday_has_no_target_and_no_minus_hours():
    profile = EmployeeProfile(weekly_target_minutes=2400, workdays="12345")
    week = summarize_period([], date(2026, 6, 1), date(2026, 6, 7), profile, date(2026, 11, 2), datetime(2026, 11, 2, 12),
                            holidays=holidays_for(2026, "NW"))
    assert week.target_seconds == 32 * H and week.balance_seconds == -32 * H
    assert next(d for d in week.days if d.day == date(2026, 6, 4)).holiday == "Fronleichnam"


# --- Krankheit: Berechnung ------------------------------------------------------------------------


def test_single_sick_day_and_parttime_week(app, staff):
    admin = login(app, "admin-a@example.com")
    r = _add(admin, staff.dennis_id, "2026-10-13")  # Dienstag, Vollzeit
    assert r.status_code == 302
    day = _summary(staff.dennis_id, date(2026, 10, 13), date(2026, 10, 13)).days[0]
    assert (day.target_seconds, day.sick_seconds, day.balance_seconds) == (8 * H, 8 * H, 0)

    # Teilzeit Mo/Di/Do je 4 Std.: Mo-Fr krank = 3 ausgefallene Arbeitstage, 12 Krankheitsstunden
    html = _add(admin, staff.laura_id, "2026-10-05", "2026-10-09", follow=True).get_data(as_text=True)
    assert "eingetragen: 05.10.2026 – 09.10.2026" in html
    week = _summary(staff.laura_id, date(2026, 10, 5), date(2026, 10, 11))
    assert week.sick_days == 3 and week.sick_seconds == 12 * H and week.target_seconds == 12 * H and week.balance_seconds == 0
    overview = admin.get("/zeiterfassung/abwesenheiten?monat=2026-10").get_data(as_text=True)
    row = re.search(r'data-abs-row="\d+">.*?Laura.*?data-abs-days>(\d+)<.*?data-abs-hours>([\d:]+) Std\.', overview, re.S)
    assert row and row.groups() == ("3", "12:00")
    assert "Grundlage zur Prüfung des Erstattungsantrags" in overview and "Zu prüfen" in overview
    month = _month(admin, staff.laura_id, "2026-10")
    assert month.count("data-day-sick") == 5 and "Krank/Urlaub" in month


def test_period_across_month_end(app, staff):
    admin = login(app, "admin-a@example.com")
    _add(admin, staff.laura_id, "2026-09-28", "2026-10-02")  # Mo 28.9. - Fr 2.10.
    sept = admin.get("/zeiterfassung/abwesenheiten?monat=2026-09").get_data(as_text=True)
    octo = admin.get("/zeiterfassung/abwesenheiten?monat=2026-10").get_data(as_text=True)
    assert re.search(r"data-abs-days>2<.*?data-abs-hours>8:00 Std\.", sept, re.S)  # Mo, Di
    assert re.search(r"data-abs-days>1<.*?data-abs-hours>4:00 Std\.", octo, re.S)  # Do 1.10.
    assert "gesamt 3 Tage / 12:00 Std." in octo
    assert _summary(staff.laura_id, date(2026, 9, 1), date(2026, 9, 30)).sick_seconds == 8 * H


def test_partially_worked_day_counts_only_remaining_hours(app, staff):
    admin = login(app, "admin-a@example.com")
    _work(staff.laura_id, date(2026, 10, 12), "09:00", "10:30", staff.admin_a_id)  # Montag, 1,5 Std. gearbeitet
    html = _add(admin, staff.laura_id, "2026-10-12", follow=True).get_data(as_text=True)
    assert "bereits Arbeitszeit erfasst" in html and "Sie bleibt erhalten" in html
    day = _summary(staff.laura_id, date(2026, 10, 12), date(2026, 10, 12)).days[0]
    assert (day.net_seconds, day.sick_seconds, day.balance_seconds) == (int(1.5 * H), int(2.5 * H), 0)
    # Mehr gearbeitet als Soll: keine Krankheitsstunden, Plusstunden bleiben
    _work(staff.dennis_id, date(2026, 10, 14), "08:00", "17:30", staff.admin_a_id)
    _add(admin, staff.dennis_id, "2026-10-14")
    day = _summary(staff.dennis_id, date(2026, 10, 14), date(2026, 10, 14)).days[0]
    assert day.sick_seconds == 0 and day.balance_seconds == int(1.5 * H)


def test_holiday_and_vacation_overlap(app, staff):
    admin = login(app, "admin-a@example.com")
    html = _add(admin, staff.laura_id, "2026-06-01", "2026-06-05", follow=True).get_data(as_text=True)
    assert "Fronleichnam" in html
    week = _summary(staff.laura_id, date(2026, 6, 1), date(2026, 6, 7))
    assert week.sick_days == 2 and week.sick_seconds == 8 * H and week.balance_seconds == 0  # Do = Feiertag

    # Urlaub Mo-Fr, danach Krankheit Mi-Do: Do zaehlt als Krankheit, Mo/Di als Urlaub
    assert _add(admin, staff.laura_id, "2026-10-19", "2026-10-23", kind="vacation").status_code == 302
    html = _add(admin, staff.laura_id, "2026-10-21", "2026-10-22", follow=True).get_data(as_text=True)
    assert "war Urlaub eingetragen" in html
    week = _summary(staff.laura_id, date(2026, 10, 19), date(2026, 10, 25))
    assert (week.vacation_seconds, week.sick_seconds, week.balance_seconds) == (8 * H, 4 * H, 0)
    # Urlaub auf Krankheitstage: abgelehnt
    html = _add(admin, staff.laura_id, "2026-06-03", "2026-06-08", kind="vacation", follow=True).get_data(as_text=True)
    assert "nicht auf Krankheitstage gelegt" in html
    assert _absence(kind="vacation", start_date=date(2026, 6, 3)) is None


def test_no_double_sick_entry(app, staff):
    admin = login(app, "admin-a@example.com")
    _add(admin, staff.laura_id, "2026-10-05", "2026-10-09")
    html = _add(admin, staff.laura_id, "2026-10-08", "2026-10-12", follow=True).get_data(as_text=True)
    assert "bereits Krankheit eingetragen (05.10.2026 – 09.10.2026)" in html
    _add(admin, staff.laura_id, "2026-10-05", "2026-10-09")  # Doppelklick
    with bypass_tenant_scope():
        assert TimeAbsence.query.filter_by(user_id=staff.laura_id, deleted_at=None).count() == 1
    assert "nicht vor dem Beginn" in _add(admin, staff.laura_id, "2026-10-20", "2026-10-19", follow=True).get_data(as_text=True)


def test_correction_and_removal_recalculate_and_are_logged(app, staff):
    admin = login(app, "admin-a@example.com")
    _add(admin, staff.laura_id, "2026-10-05", "2026-10-09")
    absence = _absence(user_id=staff.laura_id)
    edit = admin.get(f"/zeiterfassung/abwesenheiten/{absence.id}/bearbeiten").get_data(as_text=True)
    assert "3 ausgefallene Arbeitstage" in edit
    r = admin.post(f"/zeiterfassung/abwesenheiten/{absence.id}/bearbeiten", data={"start_date": "2026-10-05", "end_date": "2026-10-06"})
    assert r.status_code == 302
    assert _summary(staff.laura_id, date(2026, 10, 5), date(2026, 10, 11)).sick_seconds == 8 * H
    assert admin.post(f"/zeiterfassung/abwesenheiten/{absence.id}/status", data={"status": "applied"}).status_code == 302
    assert _absence(id=absence.id).refund_status == "applied"
    assert "Beantragt" in admin.get("/zeiterfassung/abwesenheiten?monat=2026-10").get_data(as_text=True)
    admin.post(f"/zeiterfassung/abwesenheiten/{absence.id}/status", data={"status": "irgendwas"})
    assert _absence(id=absence.id).refund_status == "applied"

    assert admin.post(f"/zeiterfassung/abwesenheiten/{absence.id}/entfernen").status_code == 302
    week = _summary(staff.laura_id, date(2026, 10, 5), date(2026, 10, 11))
    assert week.sick_seconds == 0 and week.balance_seconds == -12 * H
    with bypass_tenant_scope():
        log = TimeCorrection.query.filter_by(target_type="absence", target_id=absence.id).order_by(TimeCorrection.id).all()
        assert [e.action for e in log] == ["create", "update", "status", "void"]
        assert log[1].old_value == "Krank 05.10.2026 – 09.10.2026" and log[1].new_value == "Krank 05.10.2026 – 06.10.2026"
        assert all(e.corrected_by_email_snapshot == "admin-a@example.com" and e.created_at for e in log)
    protocol = admin.get(f"/zeiterfassung/protokoll?mitarbeiter={staff.laura_id}").get_data(as_text=True)
    assert "Krankheit/Urlaub" in protocol and "Status geändert" in protocol and "Entfernt" in protocol
    assert admin.post(f"/zeiterfassung/abwesenheiten/{absence.id}/entfernen").status_code == 404


# --- Rechte, Mitarbeiteransicht, Export --------------------------------------------------------


def test_permissions_and_employee_view(app, staff):
    admin = login(app, "admin-a@example.com")
    _add(admin, staff.laura_id, "2026-10-05", "2026-10-09")
    absence = _absence(user_id=staff.laura_id)
    laura = login(app, "laura@example.com")
    assert _add(laura, staff.laura_id, "2026-10-12").status_code == 403
    assert laura.get("/zeiterfassung/abwesenheiten").status_code == 403
    assert laura.post(f"/zeiterfassung/abwesenheiten/{absence.id}/status", data={"status": "refunded"}).status_code == 403
    own = laura.get("/zeiterfassung/monat?monat=2026-10").get_data(as_text=True)
    assert "data-day-sick" in own and "data-own-absences" in own and "Zu prüfen" not in own
    assert "data-own-absences" not in login(app, "dennis@example.com").get("/zeiterfassung/monat?monat=2026-10").get_data(as_text=True)
    other = login(app, "admin-b@example.com")
    assert other.post(f"/zeiterfassung/abwesenheiten/{absence.id}/entfernen").status_code == 404
    assert _add(other, staff.laura_id, "2026-10-12").status_code == 404
    assert _absence(id=absence.id).deleted_at is None


def test_export_contains_sick_hours_and_basis_note(app, staff):
    admin = login(app, "admin-a@example.com")
    _add(admin, staff.laura_id, "2026-10-05", "2026-10-09")
    _add(admin, staff.laura_id, "2026-10-19", "2026-10-19", kind="vacation")
    csv_text = admin.get(f"/zeiterfassung/team/{staff.laura_id}/export.csv?monat=2026-10").get_data().decode("utf-8-sig")
    lines = csv_text.splitlines()
    assert next(row for row in lines if row.startswith("Datum")).split(";")[8:10] == ["Krank", "Urlaub"]
    monday = next(row for row in lines if row.startswith("05.10.2026")).split(";")
    assert monday[8] == "4:00" and monday[10] == "Krank"
    assert "Krankheitszeiträume (Grundlage zur Prüfung des Erstattungsantrags)" in csv_text
    assert "05.10.2026 – 09.10.2026;3;12:00;Zu prüfen" in csv_text
    assert "ergibt sich daraus nicht" in csv_text
    pdf = admin.get(f"/zeiterfassung/team/{staff.laura_id}/export.pdf?monat=2026-10")
    assert pdf.status_code == 200 and pdf.data[:4] == b"%PDF"
    team = admin.get("/zeiterfassung/team/export.csv?monat=2026-10").get_data().decode("utf-8-sig")
    laura_row = next(row for row in team.splitlines() if row.startswith("Laura"))
    assert laura_row.split(";")[-5:] == ["3", "12:00", "1", "4:00", ""]  # Resturlaub: kein Anspruch hinterlegt


def test_holiday_state_setting(app, staff):
    admin = login(app, "admin-a@example.com")
    html = admin.get("/zeiterfassung/abwesenheiten?monat=2026-10").get_data(as_text=True)
    assert 'value="NW" selected' in html and "Reformationstag" not in html
    admin.post("/zeiterfassung/abwesenheiten/bundesland", data={"state": "TH", "monat": "2026-10"})
    html = admin.get("/zeiterfassung/abwesenheiten?monat=2026-10").get_data(as_text=True)
    assert "Reformationstag" in html
    assert login(app, "laura@example.com").post("/zeiterfassung/abwesenheiten/bundesland", data={"state": "BY"}).status_code == 403
