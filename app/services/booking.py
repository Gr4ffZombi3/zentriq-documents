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
            alternatives = availability.free_slots(assignee, minutes, count=3, s=s)[0] if ready else []
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
