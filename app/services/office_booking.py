"""Allgemeiner Buchungslink je Buero (/termin/<slug>) - ohne Anmeldung, ohne KI.

Frei ist eine Startzeit, wenn (availability.bookable_days - dieselbe Berechnung wie allgemeine Buchungslinks)
- sie in der Verfuegbarkeit aus dem Kalender liegt (Buerozeiten des Wochentags, Terminplanung) und der
  GESAMTE Termin hineinpasst (Ortszeit Europe/Berlin, je Tag umgerechnet - Sommer-/Winterzeit stimmt).
  Eigene Zeitfenster fuer die Online-Buchung gibt es nicht mehr (nichts doppelt pflegen),
- sie in der Zukunft liegt (plus Mindestvorlauf aus den Buero-Einstellungen),
- der Tag ein Arbeitstag des zustaendigen Mitarbeiters ist (falls hinterlegt),
- sich kein geplanter Termin des zustaendigen Kalenders inkl. Puffer ueberschneidet. Abwesenheiten
  und Sperrzeiten sind Kalendertermine und blockieren damit ebenfalls.

Oeffentlich ausgegeben werden nur diese freien Uhrzeiten und das Bueroprofil - nie Termine, Kunden-
oder Mitarbeiterdaten. Gebucht wird unter derselben Sperre wie interne Termine und persoenliche
Buchungslinks (zentriq-cal-<buero>-<mitarbeiter>), in neuer Transaktion erneut geprueft. Jede
Formularanzeige hat eine eigene Kennung (client_ref "ob-<kennung>"): Doppelklick bzw. erneutes Senden
ergibt denselben Termin. Der Link bleibt nach der Buchung gueltig."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from flask import current_app, has_request_context, request

from app.extensions import db
from app.models import OfficeBookingPage, OfficeSettings, Tenant, User
from app.services.customer_lock import CustomerLockTimeout, NamedLock, fresh_transaction
from app.services.timetracking.clock import local_to_utc_naive, to_local
from app.tenancy import bypass_tenant_scope, use_tenant_id

STEP_MINUTES = 30
MAX_HORIZON = 60
DAY_NAMES = ("Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag")
_SLUG = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,38}[a-z0-9])?$")
RESERVED = {"buchen", "admin", "api", "static", "login", "neu", "test"}
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[a-z]{2,}$", re.I)
_PHONE = re.compile(r"^[+()0-9 /-]{6,30}$")


class BookingError(Exception):
    def __init__(self, message: str, code: str = "invalid"):
        super().__init__(message)
        self.code = code


# --- Einstellungen -------------------------------------------------------------------------------


def slugify(value: str | None) -> str:
    text = unicodedata.normalize("NFKD", (value or "").replace("ß", "ss")).encode("ascii", "ignore").decode("ascii").lower()
    return re.sub(r"-{2,}", "-", re.sub(r"[^a-z0-9]+", "-", text)).strip("-")[:40]


def page_for_office() -> OfficeBookingPage | None:
    return OfficeBookingPage.query.first()


def suggested_slug(user: User) -> str:
    """Vorschlag: Nachname des Buero-Admins bzw. Buero-Name, eindeutig gemacht."""
    from app.services.calendar import member_name

    tenant = db.session.get(Tenant, user.tenant_id)
    s = OfficeSettings.query.first()
    base = slugify(member_name(user).split()[-1]) if member_name(user) else ""
    base = base or slugify(s.office_name if s else "") or slugify(tenant.slug if tenant else "") or "buero"
    candidate, n = base, 2
    with bypass_tenant_scope():
        while candidate in RESERVED or OfficeBookingPage.query.filter(OfficeBookingPage.slug == candidate).first() is not None:
            candidate, n = f"{base}-{n}", n + 1
    return candidate


def parse_hm(value) -> time | None:
    if not isinstance(value, str) or not re.fullmatch(r"\d{1,2}:\d{2}", value.strip()):
        return None
    h, m = (int(x) for x in value.strip().split(":"))
    return time(h, m) if h < 24 and m < 60 else None


def windows_of(page: OfficeBookingPage) -> dict[int, tuple[time, time]]:
    """Verfuegbarkeit aus dem Kalender fuer den zustaendigen Kalender (Buerozeiten x Arbeitstage)."""
    from app.services.availability import calendar_windows

    assignee = db.session.get(User, page.assignee_user_id) if page.assignee_user_id else None
    return calendar_windows(assignee) if assignee is not None else {}


def save_settings(user: User, form) -> OfficeBookingPage:
    """Nur Buero-Admin (Aufrufer prueft). Wirft BookingError mit verstaendlicher Meldung."""
    from app.services import calendar as cal

    page = page_for_office()
    slug = slugify(form.get("slug") or "")
    if not _SLUG.match(slug or "") or slug in RESERVED:
        raise BookingError("Bitte eine Adresse aus Kleinbuchstaben, Ziffern und Bindestrich angeben (z. B. „heller“).")
    with bypass_tenant_scope():
        taken = OfficeBookingPage.query.filter(OfficeBookingPage.slug == slug).first()
    if taken is not None and (page is None or taken.id != page.id):
        raise BookingError("Diese Adresse ist bereits vergeben. Bitte eine andere wählen.")
    title = " ".join((form.get("title") or "").split())[:80] or "Beratungstermin"
    try:
        minutes = int(form.get("minutes") or 0)
        horizon = int(form.get("horizon_days") or 21)
    except ValueError:
        raise BookingError("Bitte Dauer und Vorausschau als Zahl angeben.") from None
    if not 10 <= minutes <= 240:
        raise BookingError("Die Dauer muss zwischen 10 und 240 Minuten liegen.")
    if not 1 <= horizon <= MAX_HORIZON:
        raise BookingError(f"Die Vorausschau muss zwischen 1 und {MAX_HORIZON} Tagen liegen.")
    members = {m.id: m for m in cal.office_members()}
    try:
        assignee_id = int(form.get("assignee_id") or 0)
    except ValueError:
        assignee_id = 0
    if assignee_id not in members:
        raise BookingError("Bitte den Kalender auswählen, in dem die Termine landen.")
    enabled = form.get("enabled") in ("1", "on", "true")
    from app.services.availability import calendar_windows

    if enabled and not calendar_windows(members[assignee_id]):
        raise BookingError("Für diesen Kalender ist keine Verfügbarkeit hinterlegt. Bitte zuerst unter „Terminplanung“ "
                           "die Bürozeiten (und ggf. die Arbeitstage des Mitarbeiters) eintragen.")
    if page is None:
        page = OfficeBookingPage(tenant_id=user.tenant_id)
        db.session.add(page)
    page.slug, page.title, page.minutes, page.horizon_days = slug, title, minutes, horizon
    page.assignee_user_id, page.enabled = assignee_id, enabled  # page.windows: frueher eigene Fenster, ungenutzt
    page.updated_by_user_id = user.id
    if "page_title" in form:
        settings = OfficeSettings.query.first()
        if settings is None:
            settings = OfficeSettings(tenant_id=user.tenant_id)
            db.session.add(settings)
        settings.booking_title = clean_title(form.get("page_title"))
        settings.updated_by_user_id = user.id
    db.session.commit()
    return page


def public_url(page: OfficeBookingPage) -> str:
    base = (current_app.config.get("PUBLIC_URL") or "").rstrip("/")
    if not base and has_request_context():
        base = request.url_root.rstrip("/")
    return f"{base}/termin/{page.slug}"


def find(slug: str | None) -> OfficeBookingPage | None:
    if not slug or not _SLUG.match(slug):
        return None
    with bypass_tenant_scope():
        return OfficeBookingPage.query.filter(OfficeBookingPage.slug == slug).first()


def usable(page: OfficeBookingPage) -> bool:
    if not page.enabled or not page.assignee_user_id or not windows_of(page):
        return False
    assignee = db.session.get(User, page.assignee_user_id)
    return bool(assignee and assignee.is_active and not assignee.deleted_at and assignee.tenant_id == page.tenant_id)


# --- Freie Zeiten (Aufruf im Mandanten-Kontext des Bueros) --------------------------------------


def days(page: OfficeBookingPage, now: datetime | None = None) -> list[tuple[date, list[datetime]]]:
    """Alle Tage des Buchungszeitraums (auch ohne Verfuegbarkeit - dann leer) mit freien Startzeiten (UTC, naiv)."""
    from app.services.availability import bookable_days

    assignee = db.session.get(User, page.assignee_user_id) if page.assignee_user_id else None
    if assignee is None:
        return []
    return bookable_days(assignee, page.minutes, min(int(page.horizon_days or 21), MAX_HORIZON), now=now)


def is_free(page: OfficeBookingPage, start: datetime, now: datetime | None = None) -> bool:
    day = to_local(start).date()
    return any(start in times for d, times in days(page, now) if d == day)


TITLE_MAX = 120


def clean_title(value: str | None) -> str | None:
    """Eigener Seitentitel: eine Zeile, hoechstens 120 Zeichen; leer = Standardtitel."""
    return " ".join((value or "").split())[:TITLE_MAX] or None


def custom_title(settings) -> str | None:
    return clean_title(getattr(settings, "booking_title", None)) if settings is not None else None


def public_view(page: OfficeBookingPage) -> dict:
    """Nur Bueroprofil und Termintyp - keine Mitarbeiter-, Kunden- oder Kalenderdaten."""
    tenant = db.session.get(Tenant, page.tenant_id)
    s = OfficeSettings.query.first()
    office = (s.office_name if s and s.office_name else (tenant.name if tenant else "")) or ""
    return {
        "heading": custom_title(s) or (f"Termin bei {office} buchen" if office else "Termin buchen"),
        "custom_heading": bool(custom_title(s)),
        "person": None, "title": page.title, "kind": None, "minutes": page.minutes, "office": office,
        "address": (s.address or "").strip() if s else "", "phone": (s.phone or "").strip() if s else "",
        "email": (s.email or "").strip() if s else "", "needs_contact": True,
        "logo_url": _logo(s), "logo_abs": _logo(s, external=True),
    }


def clean_contact(name: str | None, phone: str | None, email: str | None) -> dict:
    name = " ".join((name or "").split())[:120]
    phone = " ".join((phone or "").split())[:30]
    email = (email or "").strip()[:255]
    if len(name) < 2:
        raise BookingError("Bitte Ihren Namen angeben.", "contact")
    if not phone and not email:
        raise BookingError("Bitte eine Telefonnummer oder E-Mail-Adresse angeben, damit wir Sie erreichen können.", "contact")
    if phone and not _PHONE.match(phone):
        raise BookingError("Bitte eine gültige Telefonnummer angeben.", "contact")
    if email and not _EMAIL.match(email):
        raise BookingError("Bitte eine gültige E-Mail-Adresse angeben.", "contact")
    return {"name": name, "phone": phone, "email": email}


@dataclass
class Booked:
    start: datetime
    created: bool
    event_id: int | None = None


def book(page_id: int, tenant_id: int, start_value: str, contact: dict, submission: str | None) -> Booked:
    """Bucht genau einen Termin. BookingError(code): taken (inzwischen belegt/nicht frei), gone (Link aus),
    busy (Kalender wird gerade geaendert), invalid."""
    from app.services import calendar as cal

    ref = re.sub(r"[^A-Za-z0-9_-]", "", submission or "")[:40]
    if len(ref) < 8:
        raise BookingError("Die Seite ist veraltet. Bitte neu laden und erneut buchen.", "invalid")
    try:
        local = datetime.strptime((start_value or "").strip()[:16], "%Y-%m-%dT%H:%M")
    except ValueError:
        raise BookingError("Bitte eine Uhrzeit auswählen.", "invalid") from None
    with use_tenant_id(tenant_id):
        page = OfficeBookingPage.query.filter(OfficeBookingPage.id == page_id).first()
        if page is None or not usable(page):
            raise BookingError("Die Online-Buchung ist derzeit nicht verfügbar. Bitte wenden Sie sich an das Büro.", "gone")
        assignee_id = page.assignee_user_id
        lock = NamedLock(f"zentriq-cal-{tenant_id}-{assignee_id}", 15)
        try:
            lock.acquire()
        except CustomerLockTimeout:
            raise BookingError("Gerade wird ein anderer Termin gespeichert. Bitte in einem Moment erneut versuchen.", "busy") from None
        try:
            fresh_transaction()
            page = OfficeBookingPage.query.filter(OfficeBookingPage.id == page_id).first()
            assignee = db.session.get(User, assignee_id)
            client_ref = f"ob-{ref}"
            existing = cal.CalendarEvent.query.filter_by(created_by_user_id=assignee_id, client_ref=client_ref).first()
            if existing is not None:
                return Booked(existing.starts_at, False, existing.id)  # Doppelklick / erneut gesendet: gleiche Bestaetigung
            start = local_to_utc_naive(local.date(), local.time())
            if page is None or not usable(page) or not is_free(page, start):
                raise BookingError("Diese Uhrzeit ist leider nicht mehr frei. Bitte wählen Sie eine der aktuell freien Zeiten.", "taken")
            note = "Gebucht über den allgemeinen Buchungslink (ohne Anmeldung).\nKontakt: " + ", ".join(
                v for v in (contact.get("name"), contact.get("phone"), contact.get("email")) if v)
            event, created, _conflicts = cal.create_event(
                assignee,
                {"title": f"{page.title} – {contact.get('name', '')}"[:200], "start": local.strftime("%Y-%m-%dT%H:%M"),
                 "end": cal.local_iso(start + timedelta(minutes=page.minutes)), "assignee_id": assignee_id,
                 "customer_id": None, "location_kind": "", "note": note},
                client_ref=client_ref,
            )
            current_app.logger.info("office_booking.booked page=%s event=%s created=%s", page.id, event.id, created)
            return Booked(event.starts_at, created, event.id)
        except Exception:
            db.session.rollback()
            raise
        finally:
            lock.release()


def _logo(settings, external: bool = False):
    from app.services.office_logo import url_of

    return url_of(settings, external=external)
