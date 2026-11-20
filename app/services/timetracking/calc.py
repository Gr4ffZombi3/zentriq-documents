"""Reine Rechenfunktionen der Zeiterfassung (ohne DB-Zugriff, gut testbar).

Regeln:
- Netto-Arbeitszeit = Buchungsdauer minus Pausen; stornierte Eintraege zaehlen nicht.
- Laufende Buchungen/Pausen zaehlen bis `now`.
- Unvollstaendige Buchungen (Ausstempeln fehlte) zaehlen 0, bis sie korrigiert sind.
- Soll pro Arbeitstag = Wochensoll / Anzahl Arbeitstage; gezaehlt fuer Tage bis einschl. heute.
- Gesetzlicher Feiertag: Tagessoll 0 (kein Arbeits-, Krankheits- oder Urlaubstag).
- Krankheit/Urlaub: angerechnet werden nur die verbleibenden Sollstunden des Tages
  (Tagessoll minus bereits geleistete Arbeitszeit, nie negativ). Geleistete Stunden bleiben
  erhalten; ein Krankheitstag erzeugt dadurch weder Minus- noch Plusstunden.
- Liegen Krankheit und Urlaub auf demselben Tag, zaehlt der Tag als Krankheit.
"""

import calendar
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta


def _seconds_between(start: datetime, end: datetime) -> int:
    return max(0, int((end - start).total_seconds()))


def break_seconds(work_break, now: datetime) -> int:
    if work_break.voided_at is not None:
        return 0
    end = work_break.ended_at or (now if work_break.is_open else None)
    if end is None:
        return 0
    return _seconds_between(work_break.started_at, end)


def session_end(session, now: datetime) -> datetime | None:
    if session.ended_at is not None:
        return session.ended_at
    return now if session.is_open else None


def session_break_seconds(session, now: datetime) -> int:
    return sum(break_seconds(item, now) for item in session.breaks)


def session_net_seconds(session, now: datetime) -> int:
    if session.voided_at is not None:
        return 0
    end = session_end(session, now)
    if end is None:
        return 0
    return max(0, _seconds_between(session.started_at, end) - session_break_seconds(session, now))


def target_seconds_for_day(profile, day: date) -> int:
    """Tagessoll: geltender Wochenplan (Ende - Beginn - Pause), sonst bisherige gleichmaessige
    Verteilung des Wochensolls auf die Arbeitstage (bis ein Plan ab einem Stichtag hinterlegt ist)."""
    if profile is None:
        return 0
    plan_for = getattr(profile, "plan_for", None)
    plan = plan_for(day) if plan_for is not None else None
    if plan is not None:
        return plan.day_minutes(day.isoweekday()) * 60
    workdays = profile.workday_numbers
    if not workdays or day.isoweekday() not in workdays:
        return 0
    return int(profile.weekly_target_minutes * 60 / len(workdays))


def week_bounds(day: date) -> tuple[date, date]:
    start = day - timedelta(days=day.weekday())
    return start, start + timedelta(days=6)


def month_bounds(day: date) -> tuple[date, date]:
    last = calendar.monthrange(day.year, day.month)[1]
    return day.replace(day=1), day.replace(day=last)


@dataclass
class DaySummary:
    day: date
    sessions: list = field(default_factory=list)
    first_start: datetime | None = None
    last_end: datetime | None = None
    break_seconds: int = 0
    net_seconds: int = 0
    target_seconds: int = 0
    is_running: bool = False
    has_incomplete: bool = False
    is_corrected: bool = False
    holiday: str | None = None  # Name des gesetzlichen Feiertags
    absence_kind: str | None = None  # "sick" | "vacation"
    absence_id: int | None = None
    absence_seconds: int = 0  # angerechnete (ausgefallene) Sollzeit

    @property
    def sick_seconds(self) -> int:
        return self.absence_seconds if self.absence_kind == "sick" else 0

    @property
    def vacation_seconds(self) -> int:
        return self.absence_seconds if self.absence_kind == "vacation" else 0

    @property
    def credited_seconds(self) -> int:
        """Geleistete plus angerechnete Stunden - Grundlage fuer die Erfuellung der Sollzeit."""
        return self.net_seconds + self.absence_seconds

    @property
    def balance_seconds(self) -> int:
        return self.credited_seconds - self.target_seconds


@dataclass
class PeriodSummary:
    start: date
    end: date
    days: list[DaySummary]

    @property
    def net_seconds(self) -> int:
        return sum(item.net_seconds for item in self.days)

    @property
    def target_seconds(self) -> int:
        return sum(item.target_seconds for item in self.days)

    @property
    def break_seconds(self) -> int:
        return sum(item.break_seconds for item in self.days)

    @property
    def sick_seconds(self) -> int:
        return sum(item.sick_seconds for item in self.days)

    @property
    def vacation_seconds(self) -> int:
        return sum(item.vacation_seconds for item in self.days)

    @property
    def sick_days(self) -> int:
        return sum(1 for item in self.days if item.sick_seconds)

    @property
    def vacation_days(self) -> int:
        return sum(1 for item in self.days if item.vacation_seconds)

    @property
    def has_absences(self) -> bool:
        return any(item.absence_kind or item.holiday for item in self.days)

    @property
    def balance_seconds(self) -> int:
        return self.net_seconds + self.sick_seconds + self.vacation_seconds - self.target_seconds

    @property
    def has_incomplete(self) -> bool:
        return any(item.has_incomplete for item in self.days)


def absence_for_day(absences, day: date):
    """Krankheit hat Vorrang vor Urlaub (Krankheit im Urlaub)."""
    hits = [item for item in absences or () if item.start_date <= day <= item.end_date]
    hits.sort(key=lambda item: 0 if item.kind == "sick" else 1)
    return hits[0] if hits else None


def summarize_period(sessions, start: date, end: date, profile, today: date, now: datetime,
                     absences=(), holidays=None) -> PeriodSummary:
    holidays = holidays or {}
    by_day: dict[date, list] = {}
    for session in sessions:
        if session.voided_at is None and start <= session.work_date <= end:
            by_day.setdefault(session.work_date, []).append(session)

    days = []
    current = start
    while current <= end:
        day_sessions = sorted(by_day.get(current, []), key=lambda item: item.started_at)
        summary = DaySummary(day=current, sessions=day_sessions)
        if day_sessions:
            summary.first_start = day_sessions[0].started_at
            ends = [session_end(item, now) for item in day_sessions if item.ended_at is not None]
            summary.last_end = max(ends) if ends else None
            summary.break_seconds = sum(session_break_seconds(item, now) for item in day_sessions)
            summary.net_seconds = sum(session_net_seconds(item, now) for item in day_sessions)
            summary.is_running = any(item.is_open for item in day_sessions)
            summary.has_incomplete = any(item.is_incomplete for item in day_sessions)
            summary.is_corrected = any(
                item.is_corrected or any(br.is_corrected for br in item.breaks) for item in day_sessions
            )
        summary.holiday = holidays.get(current)
        absence = absence_for_day(absences, current)
        if absence is not None:
            summary.absence_kind, summary.absence_id = absence.kind, absence.id
        if current <= today and summary.holiday is None:
            summary.target_seconds = target_seconds_for_day(profile, current)
            if absence is not None:
                summary.absence_seconds = max(0, summary.target_seconds - summary.net_seconds)
        days.append(summary)
        current += timedelta(days=1)
    return PeriodSummary(start=start, end=end, days=days)


def format_duration(seconds: int | None, signed: bool = False) -> str:
    """Formatiert Sekunden als H:MM (z. B. 7:45), optional mit Vorzeichen (+0:30 / −1:15)."""
    if seconds is None:
        return "–"
    sign = ""
    if signed:
        sign = "+" if seconds > 0 else ("−" if seconds < 0 else "±")
    elif seconds < 0:
        sign = "−"
    minutes = abs(int(seconds)) // 60
    return f"{sign}{minutes // 60}:{minutes % 60:02d}"
