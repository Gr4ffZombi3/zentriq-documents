"""Gemeinsamer Buerokalender: Validierung, Speicherung, Konflikte, Verlauf.

- Mandantentrennung ueber den globalen Tenant-Filter (TenantScopedMixin); Einzelobjekte werden
  von den Routen per get_or_404_scoped geladen. Zustaendige Mitarbeiter und Kunden muessen zum
  eigenen Buero gehoeren.
- Zeiten: Eingabe/Anzeige lokal (Europe/Berlin), Speicherung naives UTC. Uhrzeiten, die es
  wegen der Zeitumstellung nicht gibt, werden abgelehnt statt verschoben.
- Gleichzeitige Aenderungen: `version` muss zur gespeicherten Version passen, sonst Konflikt
  (409) statt stillem Ueberschreiben.
- Doppelte Speicherung: gleicher client_ref desselben Benutzers -> vorhandener Termin.
- Ueberschneidungen beim zustaendigen Mitarbeiter sind eine Warnung, kein Fehler."""

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from sqlalchemy.exc import IntegrityError

from app.extensions import db
from app.models import CalendarEvent, CalendarEventChange, Customer, User, UserRole
from app.services.timetracking.clock import local_to_utc_naive, to_local, utcnow_naive

LOCATION_KINDS = {"vor_ort": "Vor Ort", "telefon": "Telefon", "video": "Video"}
MAX_DURATION = timedelta(days=7)
MAX_TITLE = 200
MAX_NOTE = 2000
# Dezente, im hellen und dunklen Design lesbare Farben je Mitarbeiter (Reihenfolge stabil nach ID).
PALETTE = ("blue", "teal", "violet", "amber", "rose", "green", "slate", "orange")


class CalendarError(ValueError):
    def __init__(self, message: str, status: int = 400, code: str = "invalid"):
        super().__init__(message)
        self.status = status
        self.code = code


# --- Mitarbeiter / Farben ------------------------------------------------------------------


def office_members() -> list[User]:
    """Aktive Benutzer des aktuellen Bueros (Buero-Admins und Mitarbeiter)."""
    from app.services.user_admin import office_members_query

    return (
        office_members_query()
        .filter(User.is_active.is_(True), User.role.in_((UserRole.OFFICE_ADMIN, UserRole.EMPLOYEE)))
        .order_by(User.id)
        .all()
    )


def member_name(user: User | None) -> str:
    if user is None:
        return "–"
    profile = getattr(user, "employee_profile", None)
    if profile is not None and profile.display_name:
        return profile.display_name
    return user.email.split("@")[0]


def member_payload(members: list[User]) -> list[dict]:
    return [
        {"id": member.id, "name": member_name(member), "color": PALETTE[index % len(PALETTE)]}
        for index, member in enumerate(members)
    ]


# --- Zeiten ----------------------------------------------------------------------------------


def parse_local(value: str | None, label: str) -> datetime:
    """'YYYY-MM-DDTHH:MM' (lokal) -> naives UTC. Lehnt nicht existierende Uhrzeiten ab."""
    try:
        local = datetime.strptime((value or "").strip()[:16], "%Y-%m-%dT%H:%M")
    except ValueError as exc:
        raise CalendarError(f"Bitte {label} vollständig angeben (Datum und Uhrzeit).") from exc
    utc = local_to_utc_naive(local.date(), local.time())
    if to_local(utc).replace(tzinfo=None) != local:
        raise CalendarError(f"Die Uhrzeit für {label} gibt es an diesem Tag wegen der Zeitumstellung nicht.")
    return utc


def local_iso(value: datetime | None) -> str | None:
    return to_local(value).strftime("%Y-%m-%dT%H:%M") if value is not None else None


def local_day_bounds(start_day: date, end_day: date) -> tuple[datetime, datetime]:
    """Lokale Tage [start_day, end_day] -> UTC-Intervall (Tageswechsel inkl. Zeitumstellung)."""
    return (
        local_to_utc_naive(start_day, time(0, 0)),
        local_to_utc_naive(end_day + timedelta(days=1), time(0, 0)),
    )


# --- Abfragen ----------------------------------------------------------------------------------


def events_between(start_utc: datetime, end_utc: datetime, assignee_ids=None, include_cancelled=True) -> list[CalendarEvent]:
    query = CalendarEvent.query.filter(CalendarEvent.starts_at < end_utc, CalendarEvent.ends_at > start_utc)
    if assignee_ids:
        query = query.filter(CalendarEvent.assignee_user_id.in_(list(assignee_ids)))
    if not include_cancelled:
        query = query.filter(CalendarEvent.status == "scheduled")
    return query.order_by(CalendarEvent.starts_at, CalendarEvent.id).all()


def upcoming(limit: int = 5, assignee_id: int | None = None) -> list[CalendarEvent]:
    now = utcnow_naive()
    query = CalendarEvent.query.filter(CalendarEvent.ends_at > now, CalendarEvent.status == "scheduled")
    if assignee_id is not None:
        query = query.filter(CalendarEvent.assignee_user_id == assignee_id)
    return query.order_by(CalendarEvent.starts_at).limit(limit).all()


def conflicts_for(assignee_id: int, starts_at: datetime, ends_at: datetime, exclude_id: int | None = None) -> list[CalendarEvent]:
    query = CalendarEvent.query.filter(
        CalendarEvent.assignee_user_id == assignee_id,
        CalendarEvent.status == "scheduled",
        CalendarEvent.starts_at < ends_at,
        CalendarEvent.ends_at > starts_at,
    )
    if exclude_id is not None:
        query = query.filter(CalendarEvent.id != exclude_id)
    return query.order_by(CalendarEvent.starts_at).all()


def change_stamp() -> str:
    """Kurzer Fingerabdruck fuer die Aktualisierung anderer Clients (Polling)."""
    row = db.session.query(db.func.max(CalendarEvent.updated_at), db.func.count(CalendarEvent.id)).one()
    latest, count = row
    return f"{latest.isoformat() if latest else '-'}|{count}"


# --- Serialisierung ----------------------------------------------------------------------------


def serialize(event: CalendarEvent, user: User | None = None) -> dict:
    customer = event.customer
    return {
        "id": event.id,
        "title": event.title,
        "start": local_iso(event.starts_at),
        "end": local_iso(event.ends_at),
        "assignee_id": event.assignee_user_id,
        "assignee": member_name(event.assignee),
        "customer": {"id": customer.id, "name": customer.name} if customer is not None else None,
        # Termin zu einem Vorgang ohne Kundenakte (noch keiner Akte zugeordnet).
        "pending_case": _pending_payload(event) if customer is None and event.pending_case_id else None,
        "location_kind": event.location_kind,
        "location_label": LOCATION_KINDS.get(event.location_kind or ""),
        "location": event.location,
        "note": event.note,
        "status": event.status,
        "version": event.version,
        "created_by": member_name(event.created_by),
        "updated_by": member_name(event.updated_by),
        "updated_at": to_local(event.updated_at).strftime("%d.%m.%Y %H:%M") if event.updated_at else None,
        "can_delete": bool(user is not None and can_delete(user, event)),
    }


def _pending_payload(event: CalendarEvent) -> dict | None:
    from flask import url_for

    case = event.pending_case
    if case is None:
        return None
    return {"id": case.id, "name": case.name, "url": url_for("workspace.pending_detail", case_id=case.id)}


def conflict_payload(items: list[CalendarEvent]) -> list[dict]:
    return [
        {"id": item.id, "title": item.title, "start": local_iso(item.starts_at), "end": local_iso(item.ends_at)}
        for item in items
    ]


# --- Validierung -------------------------------------------------------------------------------


@dataclass
class EventInput:
    title: str
    starts_at: datetime
    ends_at: datetime
    assignee_id: int
    customer_id: int | None
    location_kind: str | None
    location: str | None
    note: str | None


def _clean(value, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    value = " ".join(value.split()) if limit <= 255 else value.strip()
    return value[:limit] or None


def parse_input(data: dict, members: list[User]) -> EventInput:
    title = _clean(data.get("title"), MAX_TITLE)
    if not title:
        raise CalendarError("Bitte einen Titel angeben.")
    starts_at = parse_local(data.get("start"), "den Beginn")
    ends_at = parse_local(data.get("end"), "das Ende")
    if ends_at <= starts_at:
        raise CalendarError("Das Ende muss nach dem Beginn liegen.")
    if ends_at - starts_at > MAX_DURATION:
        raise CalendarError("Ein Termin darf höchstens 7 Tage dauern.")
    try:
        assignee_id = int(data.get("assignee_id"))
    except (TypeError, ValueError) as exc:
        raise CalendarError("Bitte einen zuständigen Mitarbeiter auswählen.") from exc
    if assignee_id not in {member.id for member in members}:
        raise CalendarError("Der zuständige Mitarbeiter gehört nicht zu Ihrem Büro.")
    customer_id = data.get("customer_id")
    if customer_id in (None, "", 0):
        customer_id = None
    else:
        try:
            customer_id = int(customer_id)
        except (TypeError, ValueError) as exc:
            raise CalendarError("Ungültige Kundenangabe.") from exc
        # Tenant-Filter: Kunden anderer Bueros sind nicht auffindbar.
        if Customer.query.filter(Customer.id == customer_id).first() is None:
            raise CalendarError("Der Kunde wurde nicht gefunden.", 404, "customer")
    kind = data.get("location_kind") or None
    if kind is not None and kind not in LOCATION_KINDS:
        raise CalendarError("Ungültige Terminart.")
    return EventInput(
        title=title,
        starts_at=starts_at,
        ends_at=ends_at,
        assignee_id=assignee_id,
        customer_id=customer_id,
        location_kind=kind,
        location=_clean(data.get("location"), 255),
        note=_clean(data.get("note"), MAX_NOTE),
    )


# --- Schreiben ---------------------------------------------------------------------------------

FIELDS = ("title", "starts_at", "ends_at", "assignee_user_id", "customer_id", "location_kind", "location", "note")


def _as_json(value):
    if isinstance(value, datetime):
        return local_iso(value)
    return value


def _log(event: CalendarEvent, user: User, action: str, changes: dict | None = None) -> None:
    db.session.add(
        CalendarEventChange(
            tenant_id=event.tenant_id,
            event_id=event.id,
            action=action,
            changes=changes or None,
            actor_user_id=user.id,
            actor_email_snapshot=user.email,
        )
    )


def can_delete(user: User, event: CalendarEvent) -> bool:
    return bool(user.is_office_admin or event.created_by_user_id == user.id)


def create_event(user: User, data: dict, client_ref: str | None = None) -> tuple[CalendarEvent, bool, list[CalendarEvent]]:
    """Gibt (Termin, neu_angelegt, Ueberschneidungen) zurueck."""
    client_ref = (client_ref or "").strip()[:64] or None
    if client_ref:
        existing = CalendarEvent.query.filter_by(created_by_user_id=user.id, client_ref=client_ref).first()
        if existing is not None:
            return existing, False, []
    values = parse_input(data, office_members())
    now = utcnow_naive()
    event = CalendarEvent(
        tenant_id=user.tenant_id,
        title=values.title,
        starts_at=values.starts_at,
        ends_at=values.ends_at,
        assignee_user_id=values.assignee_id,
        customer_id=values.customer_id,
        location_kind=values.location_kind,
        location=values.location,
        note=values.note,
        client_ref=client_ref,
        created_by_user_id=user.id,
        created_at=now,
        updated_by_user_id=user.id,
        updated_at=now,
    )
    db.session.add(event)
    try:
        db.session.flush()
    except IntegrityError:
        # Gleichzeitig zweimal abgeschickt: der erste Termin gilt.
        db.session.rollback()
        existing = CalendarEvent.query.filter_by(created_by_user_id=user.id, client_ref=client_ref).first()
        if existing is not None:
            return existing, False, []
        raise
    _log(event, user, "create", {field: [None, _as_json(getattr(event, field))] for field in FIELDS if getattr(event, field) is not None})
    db.session.commit()
    return event, True, conflicts_for(event.assignee_user_id, event.starts_at, event.ends_at, exclude_id=event.id)


def _check_version(event: CalendarEvent, version) -> None:
    try:
        expected = int(version)
    except (TypeError, ValueError):
        expected = None
    if expected is None or expected != event.version:
        raise CalendarError(
            "Der Termin wurde inzwischen von jemand anderem geändert. Bitte die aktuelle Fassung prüfen.",
            409,
            "conflict",
        )


def _bump(event: CalendarEvent, user: User, expected_version: int) -> None:
    """Version atomar erhoehen: schlaegt fehl, wenn parallel jemand gespeichert hat."""
    now = utcnow_naive()
    updated = (
        CalendarEvent.query.filter(CalendarEvent.id == event.id, CalendarEvent.version == expected_version)
        .update(
            {CalendarEvent.version: expected_version + 1, CalendarEvent.updated_at: now, CalendarEvent.updated_by_user_id: user.id},
            synchronize_session=False,
        )
    )
    if updated != 1:
        db.session.rollback()
        raise CalendarError(
            "Der Termin wurde inzwischen von jemand anderem geändert. Bitte die aktuelle Fassung prüfen.",
            409,
            "conflict",
        )


def update_event(user: User, event: CalendarEvent, data: dict) -> tuple[CalendarEvent, bool, list[CalendarEvent]]:
    _check_version(event, data.get("version"))
    expected = event.version
    values = parse_input(data, office_members())
    new = {
        "title": values.title,
        "starts_at": values.starts_at,
        "ends_at": values.ends_at,
        "assignee_user_id": values.assignee_id,
        "customer_id": values.customer_id,
        "location_kind": values.location_kind,
        "location": values.location,
        "note": values.note,
    }
    changes = {
        field: [_as_json(getattr(event, field)), _as_json(value)]
        for field, value in new.items()
        if getattr(event, field) != value
    }
    if not changes:
        return event, False, conflicts_for(event.assignee_user_id, event.starts_at, event.ends_at, exclude_id=event.id)
    _bump(event, user, expected)
    for field, value in new.items():
        setattr(event, field, value)
    _log(event, user, "update", changes)
    db.session.commit()
    db.session.refresh(event)
    return event, True, conflicts_for(event.assignee_user_id, event.starts_at, event.ends_at, exclude_id=event.id)


def set_cancelled(user: User, event: CalendarEvent, cancelled: bool, version) -> CalendarEvent:
    _check_version(event, version)
    target = "cancelled" if cancelled else "scheduled"
    if event.status == target:
        return event
    _bump(event, user, event.version)
    _log(event, user, "cancel" if cancelled else "reactivate", {"status": [event.status, target]})
    event.status = target
    db.session.commit()
    db.session.refresh(event)
    return event


def delete_event(user: User, event: CalendarEvent, version) -> None:
    if not can_delete(user, event):
        raise CalendarError("Löschen dürfen nur der Ersteller und Büro-Admins. Sie können den Termin absagen.", 403, "forbidden")
    _check_version(event, version)
    _log(
        event,
        user,
        "delete",
        {field: [_as_json(getattr(event, field)), None] for field in FIELDS if getattr(event, field) is not None},
    )
    db.session.delete(event)
    db.session.commit()


def history(event_id: int) -> list[dict]:
    rows = (
        CalendarEventChange.query.filter_by(event_id=event_id).order_by(CalendarEventChange.created_at.desc(), CalendarEventChange.id.desc()).limit(30).all()
    )
    users = {user.id: user for user in User.query.filter(User.id.in_({row.actor_user_id for row in rows if row.actor_user_id})).all()} if rows else {}
    labels = {"create": "angelegt", "update": "geändert", "cancel": "abgesagt", "reactivate": "wieder aktiviert", "delete": "gelöscht"}
    return [
        {
            "action": labels.get(row.action, row.action),
            "by": member_name(users.get(row.actor_user_id)) if row.actor_user_id in users else (row.actor_email_snapshot or "–"),
            "when": to_local(row.created_at).strftime("%d.%m.%Y %H:%M"),
            "fields": sorted((row.changes or {}).keys()) if row.action == "update" else [],
        }
        for row in rows
    ]
