"""Terminbuchung bei ausdruecklichem Auftrag - gemeinsam fuer Eingabefeld und Assistent-Werkzeug.

Unter einer Sperre je Mitarbeiter: identischen Termin wiederverwenden, Verfuegbarkeit erneut
pruefen (Buerozeiten, Arbeitstage, Termine inkl. Puffer; ohne Buerozeiten nur Ueberschneidungen),
dann im gemeinsamen Kalender speichern. Es gibt nur bestaetigte Termine (keine Reservierungen)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta

from app.models import CalendarEvent
from app.services import availability
from app.services import calendar as cal
from app.services.customer_lock import CustomerLockTimeout, NamedLock, fresh_transaction


@dataclass
class BookingResult:
    event: CalendarEvent | None = None
    created: bool = False
    reason: str | None = None
    alternatives: list = field(default_factory=list)


def book(user, *, assignee, start_utc, minutes: int, customer=None, title: str, location_kind: str = "", note=None,
         client_ref: str | None = None) -> BookingResult:
    lock = NamedLock(f"zentriq-cal-{user.tenant_id}-{assignee.id}", 15)
    try:
        lock.acquire()
    except CustomerLockTimeout:
        return BookingResult(reason="Der Kalender wird gerade geändert. Bitte gleich erneut versuchen.")
    try:
        fresh_transaction()
        end_utc = start_utc + timedelta(minutes=minutes)
        twin = CalendarEvent.query.filter(
            CalendarEvent.assignee_user_id == assignee.id, CalendarEvent.status == "scheduled",
            CalendarEvent.starts_at == start_utc, CalendarEvent.ends_at == end_utc,
            CalendarEvent.customer_id == (customer.id if customer else None),
            CalendarEvent.title == title,
        ).first()
        if twin is not None:
            return BookingResult(event=twin, created=False)
        s = availability.settings()
        ready = availability.planning_ready(s)
        if ready:
            reason = availability.check(assignee, start_utc, minutes, s)
        else:
            busy = cal.conflicts_for(assignee.id, start_utc, end_utc)
            reason = f"{cal.member_name(assignee)} hat dann bereits „{busy[0].title}“." if busy else None
        if reason:
            alternatives = alternatives_near(assignee, start_utc, minutes, s) if ready else _calendar_gaps(assignee, start_utc, minutes)
            return BookingResult(reason=reason, alternatives=alternatives)
        event, created, _conflicts = cal.create_event(
            user,
            {"title": title, "start": cal.local_iso(start_utc), "end": cal.local_iso(end_utc), "assignee_id": assignee.id,
             "customer_id": customer.id if customer else None, "location_kind": location_kind or "", "note": note},
            client_ref=client_ref,
        )
        return BookingResult(event=event, created=created)
    finally:
        lock.release()


def alternatives_near(assignee, start_utc, minutes: int, s, count: int = 3) -> list:
    """Freie Alternativen: zuerst der naechste freie Termin am gewuenschten Tag ab der gewuenschten
    Uhrzeit, danach die fruehesten freien Termine der folgenden Tage."""
    from app.services.timetracking.clock import to_local

    local = to_local(start_utc)
    near = availability.Preferences(from_day=local.date(), to_day=local.date(), earliest=local.time())
    same_day, matched = availability.free_slots(assignee, minutes, near, count=1, s=s)
    out = list(same_day) if matched else []
    later = availability.Preferences(from_day=local.date() + timedelta(days=1))
    rest, _ = availability.free_slots(assignee, minutes, later, count=count, s=s)
    for slot in rest:
        if len(out) >= count:
            break
        if all(slot.start_utc != o.start_utc for o in out):
            out.append(slot)
    return out


def _calendar_gaps(assignee, start_utc, minutes: int, count: int = 3) -> list:
    """Ohne Buerozeiten: naechste freie Zeiten am selben Tag laut Kalender (15-Minuten-Raster,
    hoechstens bis 20 Uhr), ohne Vergangenheit."""
    from app.services.timetracking.clock import to_local, utcnow_naive

    found = []
    cursor = start_utc
    day = to_local(start_utc).date()
    now = utcnow_naive()
    for _ in range(4 * 12):
        cursor += timedelta(minutes=15)
        end = cursor + timedelta(minutes=minutes)
        local_end = to_local(end)
        if local_end.date() != day or (local_end.hour, local_end.minute) > (20, 0):
            break
        if cursor > now and not cal.conflicts_for(assignee.id, cursor, end):
            found.append(availability.Slot(cursor, end))
            if len(found) >= count:
                break
    return found
