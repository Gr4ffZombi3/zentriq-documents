"""Jinja-Filter fuer einheitliche Datums-/Zeit- und Dauerformate im Portal.

Gespeichert wird UTC; angezeigt wird in APP_TIMEZONE (Europe/Berlin)."""

import hashlib
import os
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
    app.jinja_env.filters["duration"] = format_duration
    app.jinja_env.filters["weekday_short"] = weekday_short
    app.jinja_env.filters["weekday_long"] = weekday_long
    app.jinja_env.filters["month_name"] = month_name
    app.jinja_env.filters["vermittlernummer"] = lambda value: format_vermittlernummer(value) or "–"
