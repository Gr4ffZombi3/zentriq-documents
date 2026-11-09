# ruff: noqa: F811  (Fixtures werden aus anderen Testmodulen importiert)
"""Wochenplan je Mitarbeiter (Beginn, Ende, Pause) mit Stichtag - Berechnung, Export, Formular."""

from datetime import date, datetime, timedelta

from app.models import EmployeeProfile, EmployeeWorkPlan
from app.services.timetracking.calc import summarize_period, target_seconds_for_day
from app.services.timetracking.clock import local_today
from app.services.timetracking.export import month_csv
from app.tenancy import bypass_tenant_scope
from tests.test_permissions_security import login, world  # noqa: F401

NOW = datetime(2026, 11, 30, 12, 0)
TODAY = date(2026, 11, 30)
H = 3600


def _profile(weekly=540, workdays="12345", plans=()):
    p = EmployeeProfile(weekly_target_minutes=weekly, workdays=workdays)
    p.plans = list(plans)  # viewonly-Beziehung im Test direkt belegen
    return p


def _plan(valid_from, days):
    return EmployeeWorkPlan(valid_from=valid_from, days=days)


def test_different_day_lengths_and_week_sum():
    plan = _plan(date(2026, 11, 2), {"1": {"start": "08:00", "end": "16:30", "break": 30},
                                     "3": {"start": "08:00", "end": "12:00", "break": 0},
                                     "5": {"start": "13:00", "end": "18:15", "break": 15}})
    p = _profile(plans=[plan])
    assert [target_seconds_for_day(p, date(2026, 11, d)) for d in range(2, 9)] == [8 * H, 0, 4 * H, 0, 5 * H, 0, 0]
    assert plan.weekly_minutes == 17 * 60
    week = summarize_period([], date(2026, 11, 2), date(2026, 11, 8), p, TODAY, NOW)
    assert week.target_seconds == 17 * H


def test_laura_plan_mo_di_do_10_to_14():
    plan = _plan(date(2026, 10, 1), {str(d): {"start": "10:00", "end": "14:00", "break": 0} for d in (1, 2, 4)})
    p = _profile(weekly=720, plans=[plan])
    assert plan.weekly_minutes == 12 * 60 and target_seconds_for_day(p, date(2026, 10, 5)) == 4 * H
    assert target_seconds_for_day(p, date(2026, 10, 7)) == 0  # Mittwoch frei
    month = summarize_period([], date(2026, 10, 1), date(2026, 10, 31), p, TODAY, NOW)
    assert month.target_seconds == 52 * H  # 13 geplante Tage x 4 Std.
    assert month_csv("Laura", None, month, TODAY).decode("utf-8-sig").splitlines()[-1].split(";")[6] == "52:00"
    assert "Mo 10:00–14:00 (4:00)" in plan.label


def test_plan_change_from_cutoff_keeps_earlier_targets():
    """Dennis: bisher 9 Std./Woche gleichmaessig (Mo-Fr je 1:48); neuer Plan ab 16.11. -
    davor unveraendert, ab Stichtag nach Plan."""
    p = _profile(weekly=540)
    before = summarize_period([], date(2026, 11, 2), date(2026, 11, 22), p, TODAY, NOW).target_seconds
    assert target_seconds_for_day(p, date(2026, 11, 2)) == int(1.8 * H)
    p.plans = [_plan(date(2026, 11, 16), {"2": {"start": "09:00", "end": "15:30", "break": 30}})]
    assert target_seconds_for_day(p, date(2026, 11, 13)) == int(1.8 * H)  # vor dem Stichtag: wie bisher
    assert target_seconds_for_day(p, date(2026, 11, 16)) == 0 and target_seconds_for_day(p, date(2026, 11, 17)) == 6 * H
    first_weeks = summarize_period([], date(2026, 11, 2), date(2026, 11, 15), p, TODAY, NOW).target_seconds
    assert first_weeks == 10 * int(1.8 * H) and before == 15 * int(1.8 * H)


def test_admin_saves_plan_with_validation_and_permissions(app, world):
    admin = login(app, "admin-a@example.com")
    url = f"/mitarbeiter/{world.laura_id}/wochenplan"
    today = local_today()
    base = {"gueltig_ab": today.isoformat()}
    laura_days = {**{f"tag_{d}": "1" for d in (1, 2, 4)}, **{f"beginn_{d}": "10:00" for d in (1, 2, 4)}, **{f"ende_{d}": "14:00" for d in (1, 2, 4)}}

    for bad, msg in (({**base, "tag_1": "1", "beginn_1": "14:00", "ende_1": "10:00"}, "Ende muss nach dem Beginn"),
                     ({**base, "tag_1": "1", "beginn_1": "10:00", "ende_1": "11:00", "pause_1": "60"}, "Pause (60 Min.) muss kürzer"),
                     ({**base, "tag_1": "1", "beginn_1": "10", "ende_1": "11:00"}, "als Uhrzeit"),
                     ({**laura_days, "gueltig_ab": (today - timedelta(days=1)).isoformat()}, "frühestens ab heute")):
        r = admin.post(url, data=bad, follow_redirects=True)
        assert msg in r.get_data(as_text=True), msg
    with bypass_tenant_scope():
        assert EmployeeWorkPlan.query.count() == 0

    assert login(app, "laura@example.com").post(url, data={**laura_days, **base}).status_code == 403
    assert login(app, "admin-b@example.com").post(url, data={**laura_days, **base}).status_code == 404
    r = admin.post(url, data={**laura_days, **base}, follow_redirects=True)
    assert "12:00 Std. pro Woche" in r.get_data(as_text=True)
    page = admin.get(f"/mitarbeiter/{world.laura_id}").get_data(as_text=True)
    assert "Mo 10:00–14:00 (4:00)" in page and 'value="10:00"' in page

    # Kuenftiger Plan entfernbar, geltender nicht
    later = (today + timedelta(days=7)).isoformat()
    admin.post(url, data={**laura_days, "gueltig_ab": later, "pause_1": "30"})
    with bypass_tenant_scope():
        current, future = EmployeeWorkPlan.query.order_by(EmployeeWorkPlan.valid_from).all()
    r = admin.post(url, data={"entfernen": current.id}, follow_redirects=True)
    assert "bereits geltender Plan kann nicht entfernt" in r.get_data(as_text=True)
    admin.post(url, data={"entfernen": future.id})
    with bypass_tenant_scope():
        assert [p.id for p in EmployeeWorkPlan.query.all()] == [current.id]


def test_without_plan_no_invented_times(app, world):
    page = login(app, "admin-a@example.com").get(f"/mitarbeiter/{world.dennis_id}").get_data(as_text=True)
    assert "Noch kein Wochenplan" in page and 'name="beginn_1" class="field-surface" aria-label="Mo Beginn" value=""' in page
