"""Reine Rechenfunktionen der Zeiterfassung (ohne DB-Zugriff, gut testbar).

Regeln:
- Netto-Arbeitszeit = Buchungsdauer minus Pausen; stornierte Eintraege zaehlen nicht.
- Laufende Buchungen/Pausen zaehlen bis `now`.
- Unvollstaendige Buchungen (Ausstempeln fehlte) zaehlen 0, bis sie korrigiert sind.
- Soll pro Arbeitstag = Wochensoll / Anzahl Arbeitstage; gezaehlt fuer Tage bis einschl. heute.
- Feiertage/Urlaub sind (noch) nicht beruecksichtigt.
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
    if profile is None:
        return 0
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

    @property
    def balance_seconds(self) -> int:
        return self.net_seconds - self.target_seconds


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
    def balance_seconds(self) -> int:
        return self.net_seconds - self.target_seconds

    @property
    def has_incomplete(self) -> bool:
        return any(item.has_incomplete for item in self.days)


def summarize_period(sessions, start: date, end: date, profile, today: date, now: datetime) -> PeriodSummary:
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
        if current <= today:
            summary.target_seconds = target_seconds_for_day(profile, current)
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
