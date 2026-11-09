"""Wochenplan je Mitarbeiter (Arbeitstag, Beginn, Ende, Pause) mit Gueltigkeitsdatum - ohne KI.

- Tagessoll = Ende - Beginn - Pause; freie Tage 0 Std.; Wochensoll = Summe.
- Gueltig ab einem Stichtag, frueheste heute: bereits vergangene Sollwerte und erfasste Zeiten
  aendern sich nicht. Bereits geltende Plaene sind unveraenderlich; kuenftige koennen ersetzt
  oder entfernt werden.
- Ohne Plan gilt weiter die bisherige gleichmaessige Verteilung (EmployeeProfile)."""

from __future__ import annotations

import re
from datetime import date

from app.extensions import db
from app.models import EmployeeWorkPlan, User
from app.models.timetracking import WEEKDAY_SHORT
from app.services.timetracking.clock import local_today

_TIME = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


class PlanError(ValueError):
    pass


def _minutes(value: str) -> int:
    h, m = value.split(":")
    return int(h) * 60 + int(m)


def parse_days(form) -> dict:
    """Formular -> {"1": {"start", "end", "break"}, ...}; prueft Beginn < Ende und Pause."""
    days = {}
    for i in range(1, 8):
        name = WEEKDAY_SHORT[i - 1]
        if form.get(f"tag_{i}") != "1":
            continue
        start, end = (form.get(f"beginn_{i}") or "").strip(), (form.get(f"ende_{i}") or "").strip()
        if not _TIME.match(start) or not _TIME.match(end):
            raise PlanError(f"{name}: Bitte Beginn und Ende als Uhrzeit angeben (z. B. 08:30).")
        start, end = f"{int(start.split(':')[0]):02d}:{start.split(':')[1]}", f"{int(end.split(':')[0]):02d}:{end.split(':')[1]}"
        raw_break = (form.get(f"pause_{i}") or "0").strip() or "0"
        if not raw_break.isdigit():
            raise PlanError(f"{name}: Die Pause bitte in ganzen Minuten angeben.")
        pause = int(raw_break)
        span = _minutes(end) - _minutes(start)
        if span <= 0:
            raise PlanError(f"{name}: Das Ende muss nach dem Beginn liegen.")
        if pause >= span:
            raise PlanError(f"{name}: Die Pause ({pause} Min.) muss kürzer sein als die Arbeitszeit ({span} Min.).")
        days[str(i)] = {"start": start, "end": end, "break": pause}
    return days


def save_plan(actor: User, member: User, valid_from: date, days: dict) -> EmployeeWorkPlan:
    if valid_from < local_today():
        raise PlanError("Der Plan kann frühestens ab heute gelten – vergangene Sollwerte bleiben unverändert.")
    plan = EmployeeWorkPlan.query.filter(EmployeeWorkPlan.user_id == member.id, EmployeeWorkPlan.valid_from == valid_from).first()
    if plan is None:
        plan = EmployeeWorkPlan(tenant_id=member.tenant_id, user_id=member.id, valid_from=valid_from)
        db.session.add(plan)
    plan.days, plan.created_by_user_id = days, actor.id
    return plan


def delete_future_plan(member: User, plan_id: int) -> None:
    plan = EmployeeWorkPlan.query.filter(EmployeeWorkPlan.id == plan_id, EmployeeWorkPlan.user_id == member.id).first()
    if plan is None:
        raise PlanError("Der Plan wurde nicht gefunden.")
    if plan.valid_from <= local_today():
        raise PlanError("Ein bereits geltender Plan kann nicht entfernt werden – bitte einen neuen Plan ab einem späteren Datum anlegen.")
    db.session.delete(plan)
