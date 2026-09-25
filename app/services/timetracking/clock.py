"""Zeitzonen-Helfer. Gespeichert wird naives UTC; Tagesgrenzen und Anzeige nutzen
APP_TIMEZONE (Standard Europe/Berlin, inkl. Sommer-/Winterzeit)."""

from datetime import date, datetime, time, timezone
from zoneinfo import ZoneInfo

from flask import current_app, has_app_context

DEFAULT_TIMEZONE = "Europe/Berlin"


def app_timezone() -> ZoneInfo:
    name = current_app.config.get("APP_TIMEZONE", DEFAULT_TIMEZONE) if has_app_context() else DEFAULT_TIMEZONE
    return ZoneInfo(name)


def utcnow_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def to_local(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc).astimezone(app_timezone())


def local_date_of(value: datetime) -> date:
    return to_local(value).date()


def local_today(now: datetime | None = None) -> date:
    return local_date_of(now or utcnow_naive())


def local_to_utc_naive(day: date, clock_time: time) -> datetime:
    local = datetime.combine(day, clock_time, tzinfo=app_timezone())
    return local.astimezone(timezone.utc).replace(tzinfo=None)


def format_utc_iso(value: datetime | None) -> str | None:
    return value.strftime("%Y-%m-%dT%H:%M:%SZ") if value is not None else None


def parse_utc_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
