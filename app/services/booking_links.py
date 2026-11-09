"""Persoenliche Buchungslinks (ohne Anmeldung, ohne KI).

Sicherheit:
- Token: secrets.token_urlsafe(32) (256 Bit), nur SHA-256 als Suchschluessel gespeichert, dazu eine
  verschluesselte Fassung zum erneuten Kopieren im Buero. Keine IDs im Link.
- Der Link erlaubt ausschliesslich: freie Zeiten DIESER Freigabe lesen und genau einen Termin buchen.
  Die oeffentliche Seite bekommt nur Terminart, Dauer, Buero-Name und Uhrzeiten - nie Kunden-,
  Kalender- oder Notizdaten (public_view).
- Frei = freigegebene Startzeit, in der Zukunft (plus Vorlauf), ohne Ueberschneidung mit Terminen
  des Mitarbeiters inkl. Puffer (Abwesenheiten sind Kalendertermine).
- Buchen unter derselben Sperre wie interne Buchungen (zentriq-cal-<buero>-<mitarbeiter>), in neuer
  Transaktion erneut geprueft; Termin und "Link genutzt" werden gemeinsam committet. Der Termin hat
  den festen client_ref "bl-<id>" (eindeutig je Buero/Ersteller) - ein zweiter Termin ueber denselben
  Link ist auch bei gleichzeitigen Anfragen ausgeschlossen."""

from __future__ import annotations

import base64
import hashlib
import re
import secrets
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from cryptography.fernet import Fernet, InvalidToken
from flask import current_app, has_request_context, request

from app.extensions import db
from app.models import BookingLink, CalendarEvent, Customer, OfficeSettings, Tenant, User
from app.services.customer_lock import CustomerLockTimeout, NamedLock, fresh_transaction
from app.services.timetracking.clock import local_to_utc_naive, to_local, utcnow_naive
from app.tenancy import bypass_tenant_scope, use_tenant_id

MAX_OFFERS = 300
MAX_DAYS = 120
STATUS_LABELS = {"active": "aktiv", "used": "genutzt", "expired": "abgelaufen", "revoked": "widerrufen"}
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[a-z]{2,}$", re.I)
_PHONE = re.compile(r"^[+()0-9 /-]{6,30}$")


class LinkError(Exception):
    def __init__(self, message: str, code: str = "invalid"):
        super().__init__(message)
        self.code = code


# --- Token ---------------------------------------------------------------------------------


def _fernet() -> Fernet:
    digest = hashlib.sha256(b"zentriq-booking-link-v1:" + current_app.config["SECRET_KEY"].encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def token_hash(token: str) -> str:
    return hashlib.sha256((token or "").encode("utf-8")).hexdigest()


PUBLIC_PATH = "/termin/buchen/"  # aeltere Links /termin/<token> leiten dorthin weiter


def public_url(link: BookingLink) -> str | None:
    """Vollstaendiger Link zum Weitergeben (immer mit Domain, damit Kopieren/WhatsApp ihn erkennen)."""
    try:
        token = _fernet().decrypt(link.token_enc.encode("ascii")).decode("ascii")
    except (InvalidToken, ValueError):
        return None
    base = (current_app.config.get("PUBLIC_URL") or "").rstrip("/")
    if not base and has_request_context():
        base = request.url_root.rstrip("/")
    return f"{base}{PUBLIC_PATH}{token}"


# --- Freigaben -----------------------------------------------------------------------------


def _parse_local(value: str) -> datetime:
    try:
        return datetime.strptime(value.strip()[:16], "%Y-%m-%dT%H:%M")
    except (ValueError, AttributeError) as exc:
        raise LinkError(f"Ungültige Zeitangabe „{value}“.") from exc


def offers_from_slots(values: list[str]) -> list[str]:
    return sorted({_parse_local(v).strftime("%Y-%m-%dT%H:%M") for v in values if v and v.strip()})


def offers_from_range(first: date, last: date, weekdays: set[int], times: list[str]) -> list[str]:
    """Zeitraum (inkl.) x Wochentage (ISO 1-7) x Uhrzeiten ("09:00")."""
    if last < first:
        raise LinkError("Das Ende des Zeitraums liegt vor dem Beginn.")
    if (last - first).days > MAX_DAYS:
        raise LinkError(f"Der Zeitraum darf höchstens {MAX_DAYS} Tage umfassen.")
    clock = []
    for t in times:
        if not re.fullmatch(r"\d{1,2}:\d{2}", t.strip()):
            raise LinkError(f"Ungültige Uhrzeit „{t}“ (Format 09:30).")
        h, m = (int(x) for x in t.strip().split(":"))
        if h > 23 or m > 59:
            raise LinkError(f"Ungültige Uhrzeit „{t}“.")
        clock.append(time(h, m))
    out, day = [], first
    while day <= last:
        if day.isoweekday() in weekdays:
            out.extend(datetime.combine(day, c).strftime("%Y-%m-%dT%H:%M") for c in clock)
        day += timedelta(days=1)
    return sorted(set(out))


def create(user: User, *, title: str, location_kind: str | None, minutes: int, assignee_id: int, customer_id: int | None,
           offers: list[str], valid_until: datetime) -> tuple[BookingLink, str]:
    from app.services import calendar as cal

    title = " ".join((title or "").split())[:120]
    if not title:
        raise LinkError("Bitte eine Terminart angeben (z. B. „Beratungsgespräch“).")
    if not 10 <= int(minutes) <= 240:
        raise LinkError("Die Termindauer muss zwischen 10 und 240 Minuten liegen.")
    if location_kind and location_kind not in cal.LOCATION_KINDS:
        raise LinkError("Ungültige Terminart.")
    members = {m.id: m for m in cal.office_members()}
    if assignee_id not in members:
        raise LinkError("Bitte einen Mitarbeiter des Büros auswählen.")
    if customer_id and Customer.query.filter(Customer.id == customer_id).first() is None:
        raise LinkError("Der Kunde wurde nicht gefunden.")
    now_local = to_local(utcnow_naive()).replace(tzinfo=None)
    offers = [o for o in offers if _parse_local(o) > now_local]
    if not offers:
        raise LinkError("Bitte mindestens eine zukünftige Uhrzeit freigeben.")
    if len(offers) > MAX_OFFERS:
        raise LinkError(f"Höchstens {MAX_OFFERS} Uhrzeiten pro Link.")
    if valid_until <= utcnow_naive():
        raise LinkError("Die Gültigkeit muss in der Zukunft liegen.")
    token = secrets.token_urlsafe(32)
    link = BookingLink(
        tenant_id=user.tenant_id, token_hash=token_hash(token), token_enc=_fernet().encrypt(token.encode("ascii")).decode("ascii"),
        title=title, location_kind=location_kind or None, minutes=int(minutes), assignee_user_id=assignee_id,
        customer_id=customer_id or None, offers=offers, expires_at=valid_until, created_by_user_id=user.id,
    )
    db.session.add(link)
    db.session.commit()
    return link, token


def status(link: BookingLink, now: datetime | None = None) -> str:
    now = now or utcnow_naive()
    if link.revoked_at is not None:
        return "revoked"
    if link.booking_count >= link.max_bookings:
        return "used"
    if link.expires_at <= now:
        return "expired"
    return "active"


def revoke(user: User, link: BookingLink) -> None:
    if link.revoked_at is None:
        link.revoked_at = utcnow_naive()
        link.revoked_by_user_id = user.id
        db.session.commit()


def booked_event(link: BookingLink) -> CalendarEvent | None:
    return CalendarEvent.query.filter(CalendarEvent.client_ref == f"bl-{link.id}").first()


# --- Oeffentlicher Zugriff -----------------------------------------------------------------


def find(token: str | None) -> BookingLink | None:
    """Link zum Token (buerouebergreifende Suche nur ueber den Hash) oder None."""
    if not token or len(token) > 100 or not re.fullmatch(r"[A-Za-z0-9_-]+", token):
        return None
    with bypass_tenant_scope():
        return BookingLink.query.filter(BookingLink.token_hash == token_hash(token)).first()


def _rules(link: BookingLink) -> tuple[int, int]:
    s = OfficeSettings.query.first()
    return (int(s.lead_minutes or 0), int(s.buffer_minutes or 0)) if s else (0, 0)


def free_times(link: BookingLink, now: datetime | None = None) -> list[datetime]:
    """Freie freigegebene Startzeiten (UTC, naiv). Aufruf im Mandanten-Kontext des Links."""
    from app.services.availability import _busy

    now = now or utcnow_naive()
    lead, buffer = _rules(link)
    out = []
    for offer in link.offers or []:
        local = _parse_local(offer)
        start = local_to_utc_naive(local.date(), local.time())
        if start < now + timedelta(minutes=lead):
            continue
        if not _busy(link.assignee_user_id, start, start + timedelta(minutes=link.minutes), buffer):
            out.append(start)
    return out


def _person(link: BookingLink) -> str | None:
    """Anzeigename des Mitarbeiters - nur der gepflegte Name, nie ein Teil der E-Mail-Adresse."""
    profile = getattr(link.assignee, "employee_profile", None) if link.assignee else None
    name = " ".join((getattr(profile, "display_name", "") or "").split())
    return name or None


def public_view(link: BookingLink) -> dict:
    """Nur das, was der Kunde sehen darf: Terminart, Mitarbeitername und das Bueroprofil
    (Name, Anschrift, Kontakt). Nie Kunden-, Kalender- oder Notizdaten - auch nicht in der Linkvorschau."""
    from app.services import calendar as cal

    tenant = db.session.get(Tenant, link.tenant_id)
    s = OfficeSettings.query.first()
    office = (s.office_name if s and s.office_name else (tenant.name if tenant else "")) or ""
    person = _person(link)
    heading = f"Termin bei {person} buchen" if person else (f"Termin bei {office} buchen" if office else "Termin buchen")
    return {
        "heading": heading,
        "person": person,
        "title": link.title,
        "kind": cal.LOCATION_KINDS.get(link.location_kind or "") if link.location_kind else None,
        "minutes": link.minutes,
        "office": office,
        "address": (s.address or "").strip() if s else "",
        "phone": (s.phone or "").strip() if s else "",
        "email": (s.email or "").strip() if s else "",
        "needs_contact": link.customer_id is None,
    }


@dataclass
class Booked:
    start: datetime
    created: bool


def _actor(link: BookingLink) -> User:
    from app.services import calendar as cal

    members = {m.id: m for m in cal.office_members()}
    actor = members.get(link.created_by_user_id) or members.get(link.assignee_user_id)
    if actor is None:
        raise LinkError("Dieser Buchungslink ist nicht mehr gültig.", "gone")
    return actor


def clean_contact(link: BookingLink, name: str | None, phone: str | None, email: str | None) -> dict:
    if link.customer_id is not None:
        return {}
    name = " ".join((name or "").split())[:120]
    phone = " ".join((phone or "").split())[:30]
    email = (email or "").strip()[:255]
    if len(name) < 2:
        raise LinkError("Bitte Ihren Namen angeben.", "contact")
    if not phone and not email:
        raise LinkError("Bitte eine Telefonnummer oder E-Mail-Adresse angeben, damit wir Sie erreichen können.", "contact")
    if phone and not _PHONE.match(phone):
        raise LinkError("Bitte eine gültige Telefonnummer angeben.", "contact")
    if email and not _EMAIL.match(email):
        raise LinkError("Bitte eine gültige E-Mail-Adresse angeben.", "contact")
    return {"name": name, "phone": phone, "email": email}


def book(link_id: int, tenant_id: int, start_value: str, contact: dict, submission: str | None) -> Booked:
    """Bucht genau einen Termin. Wirft LinkError(code) mit kundengerechter Meldung:
    gone (widerrufen/abgelaufen), used (bereits gebucht), taken (Zeit belegt/nicht frei), busy."""
    from app.services import calendar as cal

    with use_tenant_id(tenant_id):
        link = BookingLink.query.filter(BookingLink.id == link_id).first()
        if link is None:
            raise LinkError("Dieser Buchungslink ist nicht mehr gültig.", "gone")
        lock = NamedLock(f"zentriq-cal-{tenant_id}-{link.assignee_user_id}", 15)
        try:
            lock.acquire()
        except CustomerLockTimeout:
            raise LinkError("Gerade wird ein anderer Termin gespeichert. Bitte in einem Moment erneut versuchen.", "busy") from None
        try:
            fresh_transaction()
            link = BookingLink.query.filter(BookingLink.id == link_id).first()
            existing = booked_event(link)
            if existing is not None or link.booking_count >= link.max_bookings:
                if existing is not None and submission and link.last_submission == submission:
                    return Booked(existing.starts_at, False)  # doppelt abgeschickt: gleiche Bestaetigung
                raise LinkError("Über diesen Link wurde bereits ein Termin gebucht.", "used")
            if link.revoked_at is not None or link.expires_at <= utcnow_naive():
                raise LinkError("Dieser Buchungslink ist abgelaufen oder wurde zurückgezogen.", "gone")
            if start_value not in (link.offers or []):
                raise LinkError("Diese Uhrzeit ist nicht freigegeben.", "taken")
            local = _parse_local(start_value)
            start = local_to_utc_naive(local.date(), local.time())
            if start not in free_times(link):
                raise LinkError("Diese Uhrzeit ist leider nicht mehr frei.", "taken")
            actor = _actor(link)
            customer = Customer.query.filter(Customer.id == link.customer_id).first() if link.customer_id else None
            who = customer.name if customer else contact.get("name", "")
            note_lines = ["Gebucht über persönlichen Buchungslink (ohne Anmeldung)."]
            if contact:
                note_lines.append("Kontakt: " + ", ".join(v for v in (contact.get("name"), contact.get("phone"), contact.get("email")) if v))
            # Link als genutzt markieren - committet gemeinsam mit dem Termin (create_event).
            link.booking_count += 1
            link.used_at = utcnow_naive()
            link.last_submission = (submission or "")[:64] or None
            event, created, _conflicts = cal.create_event(
                actor,
                {"title": f"{link.title} – {who}"[:200], "start": local.strftime("%Y-%m-%dT%H:%M"),
                 "end": cal.local_iso(start + timedelta(minutes=link.minutes)), "assignee_id": link.assignee_user_id,
                 "customer_id": customer.id if customer else None, "location_kind": link.location_kind or "", "note": "\n".join(note_lines)},
                client_ref=f"bl-{link.id}",
            )
            if not created:
                db.session.rollback()
                raise LinkError("Über diesen Link wurde bereits ein Termin gebucht.", "used")
            current_app.logger.info("booking_link.booked link=%s event=%s", link.id, event.id)
            return Booked(event.starts_at, True)
        except Exception:
            db.session.rollback()
            raise
        finally:
            lock.release()
