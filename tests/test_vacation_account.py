# ruff: noqa: F811  (Fixtures werden aus anderen Testmodulen importiert)
"""Urlaubskonto je Mitarbeiter und Jahr. Fiktive Daten.

Laura: Teilzeit Mo/Di/Do je 4 Std., Dennis: Mo-Fr je 8 Std. Buero A in NRW."""

import re
from datetime import date, timedelta
from decimal import Decimal

from app.extensions import db as _db
from app.models import TimeCorrection, User, VacationEntitlement
from app.services.timetracking import absences
from app.services.timetracking.clock import local_today
from app.tenancy import bypass_tenant_scope
from tests.test_absences import _add, staff  # noqa: F401
from tests.test_permissions_security import login, world  # noqa: F401


def _account(user_id, year):
    with bypass_tenant_scope():
        _db.session.expire_all()
        return absences.vacation_account(_db.session.get(User, user_id), year)


def _set(client, user_id, year, days, carry="0", month=None):
    return client.post(f"/zeiterfassung/abwesenheiten/urlaubskonto/{user_id}", follow_redirects=True,
                       data={"jahr": str(year), "days": days, "carryover": carry, "monat": month or f"{year}-01"})


def test_entitlement_taken_planned_rest_parttime_holiday_and_sick(app, staff):
    admin = login(app, "admin-a@example.com")
    html = _set(admin, staff.laura_id, 2026, "18", "2,5").get_data(as_text=True)
    assert "Urlaubskonto 2026 für Laura gespeichert: Resturlaub 20,5 Tage" in html
    # Teilzeit Mo/Di/Do: Urlaub Mo 1.6.-Fr 5.6. -> Mo, Di (Do = Fronleichnam) = 2 Tage
    _add(admin, staff.laura_id, "2026-06-01", "2026-06-05", kind="vacation")
    # Urlaub Mo 19.10.-Fr 23.10., Krankheit Do 22.10. -> nur Mo, Di zaehlen = 2 Tage
    _add(admin, staff.laura_id, "2026-10-19", "2026-10-23", kind="vacation")
    _add(admin, staff.laura_id, "2026-10-22", "2026-10-22")
    acc = _account(staff.laura_id, 2026)
    assert (acc.taken, acc.planned, acc.total, acc.rest) == (4, 0, Decimal("20.5"), Decimal("16.5"))
    page = admin.get("/zeiterfassung/abwesenheiten?monat=2026-10").get_data(as_text=True)
    row = re.search(r'data-vac-row="%d".*?data-vac-taken>(\d+)<.*?data-vac-rest>\s*<strong[^>]*>([^<]+)<' % staff.laura_id, page, re.S)
    assert row and row.groups() == ("4", "16,5 Tage")


def test_planned_future_days_and_year_split(app, staff):
    admin = login(app, "admin-a@example.com")
    today = local_today()
    year = today.year + 1
    _set(admin, staff.dennis_id, year, "30")
    _set(admin, staff.dennis_id, year + 1, "30")
    # 28.12. bis 8.1. des Folgejahres: Tage je Jahr getrennt, Feiertage (1.1.) ausgenommen
    _add(admin, staff.dennis_id, f"{year}-12-28", f"{year + 1}-01-08", kind="vacation")
    first, second = _account(staff.dennis_id, year), _account(staff.dennis_id, year + 1)
    workdays = lambda a, b: sum(1 for i in range((b - a).days + 1) if (a + timedelta(i)).isoweekday() <= 5  # noqa: E731
                                and (a + timedelta(i)) not in absences.holidays_between(a, b, "NW"))
    assert first.planned == workdays(date(year, 12, 28), date(year, 12, 31)) and first.taken == 0
    assert second.planned == workdays(date(year + 1, 1, 1), date(year + 1, 1, 8))
    assert first.rest == 30 - first.planned


def test_warning_when_rest_exceeded_and_without_entitlement(app, staff):
    admin = login(app, "admin-a@example.com")
    html = _add(admin, staff.dennis_id, "2026-03-02", "2026-03-03", kind="vacation", follow=True).get_data(as_text=True)
    assert "Für 2026 ist noch kein Urlaubsanspruch hinterlegt" in html
    _set(admin, staff.dennis_id, 2026, "3")
    html = _add(admin, staff.dennis_id, "2026-03-09", "2026-03-10", kind="vacation", follow=True).get_data(as_text=True)
    assert "Achtung: Damit sind 1 Urlaubstage mehr eingetragen" in html
    assert _account(staff.dennis_id, 2026).rest == Decimal("-1")


def test_validation_permissions_log_and_views(app, staff):
    admin = login(app, "admin-a@example.com")
    assert "als Zahl angeben" in _set(admin, staff.laura_id, 2026, "viele").get_data(as_text=True)
    assert "halbe Tage" in _set(admin, staff.laura_id, 2026, "2,3").get_data(as_text=True)
    assert "zwischen 0 und 365" in _set(admin, staff.laura_id, 2026, "-1").get_data(as_text=True)
    with bypass_tenant_scope():
        assert VacationEntitlement.query.count() == 0
    _set(admin, staff.laura_id, 2026, "20")
    _set(admin, staff.laura_id, 2026, "21", "1")
    with bypass_tenant_scope():
        assert VacationEntitlement.query.count() == 1
        log = TimeCorrection.query.filter_by(target_type="entitlement").order_by(TimeCorrection.id).all()
        assert [(e.field, e.old_value, e.new_value) for e in log][-2:] == [("anspruch", "2026: 20 Tage", "2026: 21 Tage"),
                                                                          ("uebertrag", "2026: 0 Tage", "2026: 1 Tage")]
    assert "Urlaubskonto" in admin.get(f"/zeiterfassung/protokoll?mitarbeiter={staff.laura_id}").get_data(as_text=True)

    laura = login(app, "laura@example.com")
    assert laura.post(f"/zeiterfassung/abwesenheiten/urlaubskonto/{staff.laura_id}", data={"jahr": "2026", "days": "99"}).status_code == 403
    assert login(app, "admin-b@example.com").post(f"/zeiterfassung/abwesenheiten/urlaubskonto/{staff.laura_id}",
                                                   data={"jahr": "2026", "days": "99"}).status_code == 404
    assert _account(staff.laura_id, 2026).days == Decimal("21")
    own = laura.get("/zeiterfassung/monat?monat=2026-10").get_data(as_text=True)
    assert "Mein Urlaubskonto 2026" in own and "22 Tage" in own
    assert "Resturlaub 2026" in admin.get(f"/zeiterfassung/team/{staff.laura_id}?ansicht=monat&datum=2026-10-01").get_data(as_text=True)
    csv_text = admin.get(f"/zeiterfassung/team/{staff.laura_id}/export.csv?monat=2026-10").get_data().decode("utf-8-sig")
    assert "Urlaubskonto 2026: Anspruch 21 + Übertrag 1 Tage · genommen 0 · geplant 0 · Resturlaub 22 Tage" in csv_text
    team = admin.get("/zeiterfassung/team/export.csv?monat=2026-10").get_data().decode("utf-8-sig")
    assert next(r for r in team.splitlines() if r.startswith("Laura")).endswith(";22")
