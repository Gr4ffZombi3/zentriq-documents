"""Jinja-Filter fuer einheitliche Datums-/Zeit- und Dauerformate im Portal.

Gespeichert wird UTC; angezeigt wird in APP_TIMEZONE (Europe/Berlin)."""

import hashlib
import os
import re
from datetime import date, datetime

from app.services.timetracking.calc import (
    break_seconds,
    format_duration,
    session_break_seconds,
    session_net_seconds,
)
from app.services.timetracking.clock import parse_utc_iso, to_local, utcnow_naive
from app.utils.vermittlernummer import format_vermittlernummer

WEEKDAYS_SHORT = ("Mo", "Di", "Mi", "Do", "Fr", "Sa", "So")
WEEKDAYS_LONG = ("Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag")
MONTHS = (
    "Januar", "Februar", "März", "April", "Mai", "Juni",
    "Juli", "August", "September", "Oktober", "November", "Dezember",
)


def local_dt(value: datetime | None, fmt: str = "%d.%m.%Y %H:%M", default: str = "–") -> str:
    local = to_local(value)
    return local.strftime(fmt) if local is not None else default


def local_time(value: datetime | None, default: str = "–") -> str:
    return local_dt(value, "%H:%M", default)


def iso_local_dt(value: str | None, fmt: str = "%d.%m.%Y %H:%M") -> str:
    """Fuer im Korrekturprotokoll als UTC-ISO gespeicherte Werte."""
    if not value:
        return "–"
    try:
        return local_dt(parse_utc_iso(value), fmt)
    except ValueError:
        return value


def iso_date_de(value: str | None) -> str:
    """"2026-10-01" -> "01.10.2026"; unlesbare Werte unveraendert."""
    if not value:
        return "–"
    try:
        return date.fromisoformat(value[:10]).strftime("%d.%m.%Y")
    except ValueError:
        return value


_WEEKDAY_SHORT = {"1": "Mo", "2": "Di", "3": "Mi", "4": "Do", "5": "Fr", "6": "Sa", "7": "So"}


def workdays_short(value: str | None) -> str:
    """"12345" -> "Mo–Fr", "124" -> "Mo, Di, Do", "1234567" -> "Mo–So"."""
    days = sorted({ch for ch in (value or "") if ch in _WEEKDAY_SHORT})
    if not days:
        return "–"
    numbers = [int(day) for day in days]
    if len(numbers) >= 3 and numbers == list(range(numbers[0], numbers[-1] + 1)):
        return f"{_WEEKDAY_SHORT[days[0]]}–{_WEEKDAY_SHORT[days[-1]]}"
    return ", ".join(_WEEKDAY_SHORT[day] for day in days)


def hours_short(minutes: int | None) -> str:
    """Wochenstunden kompakt: 2400 Minuten -> "40 h", 2250 -> "37,5 h"."""
    if minutes is None:
        return "–"
    hours = minutes / 60
    text = f"{hours:.2f}".rstrip("0").rstrip(".").replace(".", ",")
    return f"{text} h"


def short_dt(value: datetime | None, default: str = "–") -> str:
    """Kompakter Zeitpunkt: "01.10.26 · 20:34" (Ortszeit)."""
    return local_dt(value, "%d.%m.%y · %H:%M", default)


def email_wrap(value: str | None):
    """E-Mail mit Umbruchmoeglichkeiten vor dem "@" und nach Punkten bzw. Unterstrichen statt
    mitten im Wort - lange Adressen ("vorname.nachname.abteilung@...") brechen so an
    sinnvollen Stellen und machen ihre Spalte nicht breiter als noetig."""
    from markupsafe import Markup, escape

    if not value or "@" not in value:
        return value or ""

    def breakable(part: str) -> str:
        return re.sub(r"([._])", r"\1<wbr>", str(escape(part)))

    local, domain = value.split("@", 1)
    return Markup(f"{breakable(local)}<wbr>@{breakable(domain)}")


def weekday_short(value: date) -> str:
    return WEEKDAYS_SHORT[value.weekday()]


def weekday_long(value: date) -> str:
    return WEEKDAYS_LONG[value.weekday()]


def month_name(value: date) -> str:
    return f"{MONTHS[value.month - 1]} {value.year}"


def session_net(session) -> int:
    return session_net_seconds(session, utcnow_naive())


def session_breaks(session) -> int:
    return session_break_seconds(session, utcnow_naive())


def break_duration(work_break) -> int:
    return break_seconds(work_break, utcnow_naive())


def make_static_rev(static_folder: str):
    """Versionskennung fuer statische Dateien aus dem Dateiinhalt.

    Statische Dateien werden mit ?v=... ein Jahr lang als immutable gecacht. Eine von Hand
    gepflegte Versionsnummer veraltet, sobald jemand vergisst sie zu erhoehen - dann behaelt
    der Browser altes CSS zu neuen Templates. Der Hash aendert sich mit jedem neuen Build."""
    cache: dict[str, tuple[tuple[int, int], str]] = {}

    def static_rev(filename: str) -> str:
        path = os.path.join(static_folder, filename)
        try:
            stat = os.stat(path)
        except OSError:
            return "0"
        key = (stat.st_mtime_ns, stat.st_size)
        hit = cache.get(filename)
        if hit and hit[0] == key:
            return hit[1]
        with open(path, "rb") as handle:
            digest = hashlib.sha256(handle.read()).hexdigest()[:12]
        cache[filename] = (key, digest)
        return digest

    return static_rev


def register_template_filters(app) -> None:
    app.jinja_env.globals["static_rev"] = make_static_rev(app.static_folder)
    app.jinja_env.filters["break_duration"] = break_duration
    app.jinja_env.filters["session_net"] = session_net
    app.jinja_env.filters["session_breaks"] = session_breaks
    app.jinja_env.filters["local_dt"] = local_dt
    app.jinja_env.filters["local_time"] = local_time
    app.jinja_env.filters["iso_local_dt"] = iso_local_dt
    app.jinja_env.filters["iso_date_de"] = iso_date_de
    app.jinja_env.filters["workdays_short"] = workdays_short
    app.jinja_env.filters["hours_short"] = hours_short
    app.jinja_env.filters["short_dt"] = short_dt
    app.jinja_env.filters["email_wrap"] = email_wrap
    app.jinja_env.filters["duration"] = format_duration
    app.jinja_env.filters["weekday_short"] = weekday_short
    app.jinja_env.filters["weekday_long"] = weekday_long
    app.jinja_env.filters["month_name"] = month_name
    app.jinja_env.filters["vermittlernummer"] = lambda value: format_vermittlernummer(value) or "–"
