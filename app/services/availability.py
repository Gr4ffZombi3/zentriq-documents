"""Freie Termine im Buerokalender - deterministisch, ohne KI.

Ein Zeitfenster ist frei, wenn
- es innerhalb der Buerozeiten des Tages liegt (OfficeSettings.hours, Ortszeit Europe/Berlin -
  Umrechnung je Tag, damit Sommer-/Winterzeit stimmt),
- der Tag ein Arbeitstag des Mitarbeiters ist (EmployeeProfile.workdays, falls hinterlegt),
- der Mindestvorlauf (lead_minutes) eingehalten ist,
- sich kein geplanter Termin des Mitarbeiters inkl. Puffer (buffer_minutes) davor/danach
  ueberschneidet. Abwesenheiten (Urlaub, Schule ...) werden als Termine im Kalender eingetragen
  und blockieren damit ebenfalls.
Ohne hinterlegte Buerozeiten gibt es keine Vorschlaege (PlanningMissing) - es wird nichts
angenommen."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta

from app.models import CalendarEvent, EmployeeProfile, OfficeSettings, User
from app.services.timetracking.clock import local_to_utc_naive, local_today, to_local, utcnow_naive

WEEKDAYS = ("Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag")
STEP_MINUTES = 30


class PlanningMissing(Exception):
    """Buerozeiten sind nicht hinterlegt."""


@dataclass
class Preferences:
    """Kundenwuensche aus Nachricht/Auftrag (nur ausdruecklich Genanntes)."""

    weekdays: set[int] = field(default_factory=set)  # ISO 1..7
    earliest: time | None = None
    latest_end: time | None = None
    from_day: date | None = None
    to_day: date | None = None
    labels: list[str] = field(default_factory=list)

    @property
    def any(self) -> bool:
        return bool(self.weekdays or self.earliest or self.latest_end or self.from_day or self.to_day)


@dataclass
class Slot:
    start_utc: datetime
    end_utc: datetime

    @property
    def local_start(self) -> datetime:
        return to_local(self.start_utc)

    def label(self) -> str:
        start, end = to_local(self.start_utc), to_local(self.end_utc)
        return f"{WEEKDAYS[start.weekday()]}, {start.strftime('%d.%m.%Y')}, {start.strftime('%H:%M')}–{end.strftime('%H:%M')} Uhr"

    def iso(self) -> str:
        return to_local(self.start_utc).strftime("%Y-%m-%dT%H:%M")


def settings() -> OfficeSettings | None:
    return OfficeSettings.query.first()


def _parse_hm(value) -> time | None:
    if not isinstance(value, str) or not re.fullmatch(r"\d{1,2}:\d{2}", value.strip()):
        return None
    h, m = (int(x) for x in value.strip().split(":"))
    return time(h, m) if h < 24 and m < 60 else None


def office_hours(s: OfficeSettings | None) -> dict[int, tuple[time, time]]:
    out = {}
    for key, window in ((s.hours or {}) if s else {}).items():
        if isinstance(window, (list, tuple)) and len(window) == 2:
            start, end = _parse_hm(window[0]), _parse_hm(window[1])
            if start and end and start < end and str(key).isdigit() and 1 <= int(key) <= 7:
                out[int(key)] = (start, end)
    return out


def planning_ready(s: OfficeSettings | None = None) -> bool:
    return bool(office_hours(s if s is not None else settings()))


def workdays_of(user: User) -> set[int] | None:
    profile = EmployeeProfile.query.filter(EmployeeProfile.user_id == user.id).first()
    if profile is None or not profile.workdays:
        return None
    return {int(ch) for ch in profile.workdays if ch.isdigit() and 1 <= int(ch) <= 7}


def _busy(assignee_id: int, start_utc: datetime, end_utc: datetime, buffer: int, exclude_id=None) -> list[CalendarEvent]:
    pad = timedelta(minutes=buffer)
    query = CalendarEvent.query.filter(
        CalendarEvent.assignee_user_id == assignee_id,
        CalendarEvent.status == "scheduled",
        CalendarEvent.starts_at < end_utc + pad,
        CalendarEvent.ends_at > start_utc - pad,
    )
    if exclude_id is not None:
        query = query.filter(CalendarEvent.id != exclude_id)
    return query.order_by(CalendarEvent.starts_at).all()


def check(assignee: User, start_utc: datetime, minutes: int, s: OfficeSettings | None = None, *, now: datetime | None = None,
          exclude_id=None) -> str | None:
    """None, wenn frei; sonst der verstaendliche Grund. Wirft PlanningMissing ohne Buerozeiten."""
    s = s if s is not None else settings()
    hours = office_hours(s)
    if not hours:
        raise PlanningMissing()
    now = now or utcnow_naive()
    end_utc = start_utc + timedelta(minutes=minutes)
    local_start, local_end = to_local(start_utc), to_local(end_utc)
    day = local_start.date()
    window = hours.get(day.isoweekday())
    if window is None:
        return f"{WEEKDAYS[day.weekday()]} ist kein Bürotag."
    days = workdays_of(assignee)
    if days is not None and day.isoweekday() not in days:
        return f"{WEEKDAYS[day.weekday()]} ist kein Arbeitstag von {_name(assignee)}."
    if local_start.time() < window[0] or local_end.date() != day or local_end.time() > window[1]:
        return f"Außerhalb der Bürozeiten ({window[0].strftime('%H:%M')}–{window[1].strftime('%H:%M')} Uhr)."
    if start_utc < now + timedelta(minutes=s.lead_minutes or 0):
        return "Zu kurzfristig bzw. in der Vergangenheit."
    busy = _busy(assignee.id, start_utc, end_utc, s.buffer_minutes or 0, exclude_id=exclude_id)
    if busy:
        other = busy[0]
        return f"{_name(assignee)} hat dann bereits „{other.title}“ ({to_local(other.starts_at).strftime('%H:%M')}–{to_local(other.ends_at).strftime('%H:%M')} Uhr)."
    return None


def _name(user: User) -> str:
    from app.services.calendar import member_name

    return member_name(user)


def free_slots(assignee: User, minutes: int, prefs: Preferences | None = None, *, count: int = 3,
               s: OfficeSettings | None = None, now: datetime | None = None) -> tuple[list[Slot], bool]:
    """Bis zu `count` freie Termine, moeglichst auf verschiedene Tage verteilt (fruehester freier
    Termin je Tag). Gibt (Termine, Wuensche_beruecksichtigt) zurueck: passt kein Termin zu den
    Wuenschen, wird ohne Wuensche gesucht und False geliefert."""
    s = s if s is not None else settings()
    hours = office_hours(s)
    if not hours:
        raise PlanningMissing()
    now = now or utcnow_naive()
    prefs = prefs or Preferences()
    slots = _search(assignee, minutes, prefs, count, s, hours, now)
    if slots or not prefs.any:
        return slots, True
    return _search(assignee, minutes, Preferences(), count, s, hours, now), False


def _search(assignee, minutes, prefs, count, s, hours, now) -> list[Slot]:
    today = local_today(now)
    workdays = workdays_of(assignee)
    horizon = max(1, min(int(s.horizon_days or 14), 60))
    first_day = max(today, prefs.from_day or today)
    last_day = min(today + timedelta(days=horizon), prefs.to_day or today + timedelta(days=horizon))
    found: list[Slot] = []
    day = first_day
    while day <= last_day and len(found) < count:
        iso = day.isoweekday()
        window = hours.get(iso)
        if window and (workdays is None or iso in workdays) and (not prefs.weekdays or iso in prefs.weekdays):
            start_t = max(window[0], prefs.earliest or window[0])
            end_t = min(window[1], prefs.latest_end or window[1])
            cursor = datetime.combine(day, start_t)
            limit = datetime.combine(day, end_t)
            while cursor + timedelta(minutes=minutes) <= limit:
                start_utc = local_to_utc_naive(day, cursor.time())
                if check(assignee, start_utc, minutes, s, now=now) is None:
                    found.append(Slot(start_utc, start_utc + timedelta(minutes=minutes)))
                    break  # ein Vorschlag je Tag
                cursor += timedelta(minutes=STEP_MINUTES)
        day += timedelta(days=1)
    return found


# --- Wuensche aus Text -------------------------------------------------------------------------

_WD = {"mo": 1, "di": 2, "mi": 3, "do": 4, "fr": 5, "sa": 6, "so": 7}
_WEEKDAY_WORDS = re.compile(r"\b(montag|dienstag|mittwoch|donnerstag|freitag|samstag|sonntag)s?\b", re.I)
_NOT_WEEKDAY = re.compile(r"\b(?:nicht|kein(?:e|en)?|außer|ausser)\s+(?:am\s+)?(montag|dienstag|mittwoch|donnerstag|freitag|samstag|sonntag)s?\b", re.I)


def preferences_from(text: str, today: date) -> Preferences:
    """Nur ausdrueckliche Wuensche: Wochentage, vormittags/nachmittags/abends, "ab 15 Uhr",
    "bis 12 Uhr", "naechste Woche", "ab dem 20.11.". Im Zweifel nichts."""
    p = Preferences()
    t = text or ""
    excluded = {_WD[m.group(1).lower()[:2]] for m in _NOT_WEEKDAY.finditer(t)}
    days = {_WD[m.group(1).lower()[:2]] for m in _WEEKDAY_WORDS.finditer(t)} - excluded
    if days:
        p.weekdays = days
        p.labels.append(", ".join(WEEKDAYS[d - 1] for d in sorted(days)))
    elif excluded:
        p.weekdays = {1, 2, 3, 4, 5, 6, 7} - excluded
        p.labels.append("nicht " + ", ".join(WEEKDAYS[d - 1] for d in sorted(excluded)))
    if re.search(r"\bvormittags?\b|\bmorgens\b|\bam\s+vormittag\b", t, re.I):
        p.latest_end = time(12, 0)
        p.labels.append("vormittags")
    elif re.search(r"\bnachmittags?\b|\bam\s+nachmittag\b", t, re.I):
        p.earliest = time(12, 0)
        p.labels.append("nachmittags")
    elif re.search(r"\babends?\b|\bnach\s+feierabend\b", t, re.I):
        p.earliest = time(17, 0)
        p.labels.append("abends")
    m = re.search(r"\b(?:ab|nach)\s+(\d{1,2})(?:[:.](\d{2}))?\s*uhr\b", t, re.I)
    if m and int(m.group(1)) < 24:
        p.earliest = time(int(m.group(1)), int(m.group(2) or 0))
        p.labels.append(f"ab {p.earliest.strftime('%H:%M')} Uhr")
    m = re.search(r"\b(?:bis|vor)\s+(\d{1,2})(?:[:.](\d{2}))?\s*uhr\b", t, re.I)
    if m and int(m.group(1)) < 24:
        p.latest_end = time(int(m.group(1)), int(m.group(2) or 0))
        p.labels.append(f"bis {p.latest_end.strftime('%H:%M')} Uhr")
    if re.search(r"\b(?:nächste|naechste|kommende)n?\s+woche\b", t, re.I):
        monday = today + timedelta(days=7 - today.weekday())
        p.from_day, p.to_day = monday, monday + timedelta(days=6)
        p.labels.append("nächste Woche")
    elif re.search(r"\bdiese\s+woche\b", t, re.I):
        p.to_day = today + timedelta(days=6 - today.weekday())
        p.labels.append("diese Woche")
    m = re.search(r"\bab\s+(?:dem\s+)?(\d{1,2})\.(\d{1,2})\.(\d{2,4})?", t, re.I)
    if m:
        year = int(m.group(3)) if m.group(3) else today.year
        year = year + 2000 if year < 100 else year
        try:
            start = date(year, int(m.group(2)), int(m.group(1)))
            if start < today and not m.group(3):
                start = date(year + 1, start.month, start.day)
            p.from_day = max(p.from_day or start, start)
            p.labels.append(f"ab {start.strftime('%d.%m.')}")
        except ValueError:
            pass
    return p


def minutes_from(text: str) -> int | None:
    m = re.search(r"\b(\d{1,3})\s*(?:min(?:uten)?|minütig)\b", text or "", re.I)
    if m and 10 <= int(m.group(1)) <= 240:
        return int(m.group(1))
    if re.search(r"\b(?:eine|1)\s+stunde\b", text or "", re.I):
        return 60
    if re.search(r"\bhalbe\s+stunde\b", text or "", re.I):
        return 30
    return None
