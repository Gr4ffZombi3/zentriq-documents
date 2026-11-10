"""Vorgang ohne Kundenakte: Notiz oder Termin erfassen, wenn die Kundenakte (noch) fehlt.

- Erfassen: Name (Pflicht) und, falls bekannt, Versicherungsnehmernummer. Ein offener Vorgang mit
  derselben VN (bzw. ohne VN mit demselben Namen) wird weiterverwendet statt doppelt angelegt.
- Termine landen ueber app/services/calendar.create_event im normalen Buerokalender.
- Zuordnen: beim Import der Leipziger Liste automatisch, wenn die VN genau eine Kundenakte im
  eigenen Buero trifft und der Name nicht widerspricht; sonst Auswahl passender Akten.
- Verknuepfen ist idempotent: Notizen werden genau einmal als Kundennotiz mit Originaltext,
  Verfasser und Zeitpunkt uebernommen, Termine behalten ihre Zeit und erhalten die Akte.

Mandantentrennung ueber den globalen Tenant-Filter (TenantScopedMixin). Keine KI-Aufrufe."""

from datetime import datetime, timedelta, timezone

from flask import current_app
from sqlalchemy import or_

from app.extensions import db
from app.models import (
    CalendarEvent,
    CalendarEventChange,
    Customer,
    CustomerNote,
    PendingCase,
    PendingCaseNote,
    User,
)
from app.utils.customer_keys import VN_PATTERN, name_key, name_parts, normalize_customer_number, vn_key

NOT_LINKED_LABEL = "Noch keiner Kundenakte zugeordnet"
MAX_NOTE = 5000
MAX_CANDIDATES = 8


class CaseError(ValueError):
    pass


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def vn_display(key: str | None) -> str | None:
    return f"{key[:3]}/{key[3:9]}-{key[9]}" if key and len(key) == 10 else None


def parse_vn(raw: str | None) -> str | None:
    """Leer -> None; sonst VN-Schluessel ("808123456E"). Eine Vertragsnummer (mit Endung) zaehlt
    als ihre VN. Ungueltige Angaben werden abgelehnt statt still verworfen."""
    text = (raw or "").strip()
    if not text:
        return None
    match = VN_PATTERN.search(text)
    key = vn_key(match.group(0)) if match else vn_key(text)
    if not key:
        raise CaseError("Die Versicherungsnehmernummer bitte im Format 808/123456-E angeben – oder das Feld leer lassen.")
    return key


def clean_name(raw: str | None) -> str:
    name = " ".join((raw or "").split())[:200]
    if len(name_parts(name)) == 0:
        raise CaseError("Bitte den Namen angeben (mindestens zwei Buchstaben).")
    return name


def find_or_create(user: User, name_raw: str, vn_raw: str | None) -> tuple[PendingCase, bool]:
    name = clean_name(name_raw)
    key = parse_vn(vn_raw)
    nkey = name_key(name)[:255] or None
    query = PendingCase.query.filter(PendingCase.status == "open")
    existing = (
        query.filter(PendingCase.vn_key == key).first()
        if key
        else query.filter(PendingCase.vn_key.is_(None), PendingCase.name_key == nkey).first()
    )
    if existing is not None:
        return existing, False
    case = PendingCase(
        tenant_id=user.tenant_id, name=name, name_key=nkey, vn_key=key, vn_number=vn_display(key), created_by_user_id=user.id
    )
    db.session.add(case)
    db.session.flush()
    return case, True


def add_note(user: User, case: PendingCase, text: str, client_ref: str | None = None) -> PendingCaseNote:
    text = (text or "").strip()
    if not text:
        raise CaseError("Bitte einen Notiztext eingeben.")
    if len(text) > MAX_NOTE:
        raise CaseError(f"Die Notiz ist zu lang (höchstens {MAX_NOTE} Zeichen).")
    client_ref = (client_ref or "").strip()[:64] or None
    if client_ref:
        existing = PendingCaseNote.query.filter_by(created_by_user_id=user.id, client_ref=client_ref).first()
        if existing is not None:
            return existing  # doppelt abgeschickt
    note = PendingCaseNote(tenant_id=user.tenant_id, case_id=case.id, text=text, created_by_user_id=user.id, client_ref=client_ref)
    db.session.add(note)
    db.session.flush()
    if not case.is_open and case.customer_id:
        _transfer_note(note, case.customer_id)  # Vorgang schon zugeordnet: direkt in die Akte
    return note


def book_appointment(user: User, case: PendingCase, data: dict, client_ref: str | None = None) -> CalendarEvent:
    """Termin im Buerokalender (Pruefungen und Ueberschneidungen wie im Kalender selbst)."""
    from app.services import calendar as cal

    payload = {
        "title": (data.get("title") or "").strip() or f"Termin: {case.name}",
        "start": data.get("start"),
        "end": data.get("end"),
        "assignee_id": data.get("assignee_id") or user.id,
        "customer_id": case.customer_id if not case.is_open else None,
        "location_kind": data.get("location_kind") or None,
        "location": data.get("location"),
        "note": data.get("note"),
    }
    try:
        event, created, _conflicts = cal.create_event(user, payload, client_ref=client_ref)
    except cal.CalendarError as exc:
        raise CaseError(str(exc)) from exc
    if event.pending_case_id != case.id:
        event.pending_case_id = case.id
        db.session.commit()
    return event


def events_of(case: PendingCase) -> list[CalendarEvent]:
    return CalendarEvent.query.filter(CalendarEvent.pending_case_id == case.id).order_by(CalendarEvent.starts_at).all()


# --- Suchen ---------------------------------------------------------------------------------


def search(query: str, limit: int = 8, include_linked: bool = False) -> list[PendingCase]:
    """Vorgaenge des eigenen Bueros ueber Name (Wortanfang) oder VN-Nummer (auch ohne Buchstabe)."""
    text = (query or "").strip()
    if len(text) < 2:
        return []
    q = PendingCase.query
    if not include_linked:
        q = q.filter(PendingCase.status == "open")
    number = normalize_customer_number(text)
    conditions = []
    if len(number) >= 6 and number[:6].isdigit():
        conditions.append(PendingCase.vn_key.like(f"{number}%"))
    parts = name_parts(text)
    if parts:
        conditions.append(db.and_(*[or_(PendingCase.name_key.like(f"{p}%"), PendingCase.name_key.like(f"% {p}%")) for p in parts]))
    if not conditions:
        return []
    return q.filter(or_(*conditions)).order_by(PendingCase.status.desc(), PendingCase.created_at.desc()).limit(limit).all()


def open_count() -> int:
    return PendingCase.query.filter(PendingCase.status == "open").count()


# --- Zuordnen -------------------------------------------------------------------------------


def names_compatible(left: str | None, right: str | None) -> bool:
    """Widersprechen sich zwei Namen nicht? Gleich, oder alle Bestandteile des kuerzeren stecken
    im laengeren ("Müller" passt zu "Hans Müller", "Hans Meier" nicht zu "Hans Müller")."""
    a, b = set(name_parts(left)), set(name_parts(right))
    if not a or not b:
        return True
    small, large = (a, b) if len(a) <= len(b) else (b, a)
    return small <= large


def candidates(case: PendingCase) -> list[dict]:
    """Passende Akten zur Auswahl: gleiche VN und/oder passender Name, jeweils mit Grund."""
    from app.services.customer_notes import customers_by_vn, vn_numbers

    found: dict[int, set[str]] = {}
    if case.vn_key:
        for customer_id in customers_by_vn(case.vn_key):
            found.setdefault(customer_id, set()).add("Gleiche Versicherungsnehmernummer")
    parts = name_parts(case.name)
    if parts:
        rows = Customer.query.filter(
            db.and_(*[or_(Customer.name_key.like(f"{p}%"), Customer.name_key.like(f"% {p}%")) for p in parts])
        ).limit(MAX_CANDIDATES * 2).all()
        for customer in rows:
            if names_compatible(case.name, customer.name):
                found.setdefault(customer.id, set()).add("Gleicher Name" if customer.name_key == case.name_key else "Ähnlicher Name")
    if not found:
        return []
    customers = Customer.query.filter(Customer.id.in_(list(found))).order_by(Customer.name, Customer.id).limit(MAX_CANDIDATES).all()
    result = []
    for customer in customers:
        reasons = found[customer.id]
        conflict = case.vn_key and "Gleiche Versicherungsnehmernummer" in reasons and not names_compatible(case.name, customer.name)
        result.append(
            {
                "customer": customer,
                "vn": ", ".join(vn_numbers(customer)) or None,
                "place": " ".join(v for v in (customer.postal_code, customer.city) if v) or None,
                "reasons": sorted(reasons) + (["Name weicht ab"] if conflict else []),
            }
        )
    return result


def _transfer_note(note: PendingCaseNote, customer_id: int) -> None:
    if note.customer_note_id:
        return
    customer_note = CustomerNote(
        tenant_id=note.tenant_id,
        customer_id=customer_id,
        text=note.text,
        created_by_user_id=note.created_by_user_id,
        created_at=note.created_at,
    )
    db.session.add(customer_note)
    db.session.flush()
    note.customer_note_id = customer_note.id


def link(case: PendingCase, customer: Customer, user: User | None = None, basis: str = "manual") -> bool:
    """Verknuepft den Vorgang mit der Akte. Gibt False zurueck, wenn er schon zugeordnet war
    (erneuter Import, Doppelklick) - dann wird nichts kopiert oder veraendert."""
    if not case.is_open or customer.tenant_id != case.tenant_id:
        return False
    for note in case.notes:
        _transfer_note(note, customer.id)
    for event in events_of(case):
        if event.customer_id is None:
            event.customer_id = customer.id
            db.session.add(
                CalendarEventChange(
                    tenant_id=event.tenant_id,
                    event_id=event.id,
                    action="update",
                    changes={"customer_id": [None, customer.id]},
                    actor_user_id=user.id if user else None,
                    actor_email_snapshot=user.email if user else "Leipziger-Liste-Import",
                )
            )
    case.status = "linked"
    case.customer_id = customer.id
    case.link_basis = basis
    case.linked_at = _now()
    case.linked_by_user_id = user.id if user else None
    return True


def match_open_cases() -> dict:
    """Nach dem Import der Leipziger Liste (im Import-Commit): offene Vorgaenge mit VN, die
    genau eine Akte im eigenen Buero treffen und deren Name nicht widerspricht, verknuepfen."""
    from app.services.customer_notes import customers_by_vn

    linked = review = 0
    for case in PendingCase.query.filter(PendingCase.status == "open", PendingCase.vn_key.is_not(None)).all():
        ids = customers_by_vn(case.vn_key)
        customer = db.session.get(Customer, ids[0]) if len(ids) == 1 else None
        if customer is not None and names_compatible(case.name, customer.name):
            link(case, customer, None, basis="import")
            linked += 1
        elif ids:
            review += 1
    if linked or review:
        current_app.logger.info("pending_cases.match linked=%s review=%s", linked, review)
    return {"linked": linked, "review": review}


def default_slot(now: datetime | None = None) -> tuple[str, str]:
    """Vorschlag fuer den Terminbeginn: naechste halbe Stunde (Ortszeit), Datum und Uhrzeit."""
    from app.services.timetracking.clock import to_local, utcnow_naive

    local = to_local(now or utcnow_naive()).replace(tzinfo=None, second=0, microsecond=0)
    local += timedelta(minutes=30 - local.minute % 30)
    return local.strftime("%Y-%m-%d"), local.strftime("%H:%M")
