"""Bestaetigungs-E-Mail nach einer Buchung ueber einen oeffentlichen Buchungslink.

Ablauf:
1. queue() direkt nachdem der Termin im Kalender committet ist (nur bei einer NEU angelegten Buchung -
   Doppelklick/Neuladen liefern denselben Termin mit created=False und landen hier nicht). Die Zeile
   booking_confirmations ist je Termin eindeutig; ein zweiter Aufruf aendert nichts.
2. Celery-Aufgabe deliver(): beansprucht die Zeile atomar (pending -> sending). Nur wer das schafft,
   sendet - mehrfach eingeplante Aufgaben senden nie doppelt. Kein automatisches Wiederholen: hat der
   SMTP-Server die Mail evtl. schon angenommen, waere ein zweiter Versand moeglich.
3. Fehler: Termin bleibt gebucht; status failed + Fehlerklasse, Systemfehler-Protokoll und Hinweis
   auf der Seite "Buchungslinks" (Buero-Admin, mit "Erneut senden").

Absender: Anzeigename = Buero-Name, Adresse = MAIL_FROM (IONOS-Zugang); Reply-To = Buero-E-Mail
aus dem Bueroprofil. Der Mailtext enthaelt nur Termin- und Bueroangaben - keine internen Notizen.
Tokens/IDs gehen nicht in den Celery-Broker, nur die Zeilen-ID."""

from __future__ import annotations

import logging
from datetime import timedelta

from flask import current_app
from sqlalchemy.exc import IntegrityError

from app.extensions import db
from app.models import BookingConfirmation, CalendarEvent, OfficeSettings, Tenant
from app.services.timetracking.clock import to_local, utcnow_naive
from app.tenancy import bypass_tenant_scope, use_tenant_id

logger = logging.getLogger(__name__)
WEEKDAYS = ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"]
STUCK_AFTER = timedelta(minutes=10)  # "sending" laenger als das: Worker abgebrochen -> als Fehler zeigen
ERROR_LABELS = {
    "not_configured": "E-Mail-Versand ist nicht eingerichtet",
    "enqueue": "Versand konnte nicht eingeplant werden",
    "stuck": "Versand abgebrochen",
}


def error_label(error: str | None) -> str:
    """Verstaendliche Fehlerbeschreibung fuer das Buero (gespeichert ist die technische Fehlerklasse)."""
    error = error or ""
    if error in ERROR_LABELS:
        return ERROR_LABELS[error]
    if error.startswith("SMTPRecipientsRefused"):
        return "E-Mail-Adresse vom Mailserver abgelehnt – bitte Adresse prüfen"
    if error.startswith("SMTPAuthenticationError"):
        return "Anmeldung am Mailserver fehlgeschlagen"
    if error.startswith(("ConnectionRefused", "TimeoutError", "SMTPConnectError", "SMTPServerDisconnected", "OSError", "socket")):
        return "Mailserver nicht erreichbar"
    return "Versand fehlgeschlagen" + (f" ({error})" if error else "")


def queue(tenant_id: int, event_id: int | None, contact: dict, title: str) -> BookingConfirmation | None:
    """Plant genau eine Bestaetigung fuer diesen Termin ein. Ohne E-Mail-Adresse: nichts."""
    recipient = (contact or {}).get("email") or ""
    if not recipient or not event_id:
        return None
    with use_tenant_id(tenant_id):
        row = BookingConfirmation(tenant_id=tenant_id, event_id=event_id, recipient=recipient[:255],
                                  name=(contact.get("name") or "")[:120] or None, phone=(contact.get("phone") or "")[:30] or None,
                                  title=(title or "Termin")[:120])
        db.session.add(row)
        try:
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            return None  # fuer diesen Termin ist bereits eine Bestaetigung eingeplant
        row_id = row.id
    _enqueue(row_id, tenant_id)
    return row


def _enqueue(row_id: int, tenant_id: int) -> None:
    from app.tasks.booking_tasks import send_booking_confirmation

    try:
        send_booking_confirmation.delay(row_id)
    except Exception as exc:  # Redis weg o. Ae.: Termin bleibt, Fehler sichtbar
        logger.warning("booking_confirmation.enqueue_failed id=%s (%s)", row_id, type(exc).__name__)
        _fail(row_id, tenant_id, "enqueue", from_status=("pending",))


def _fail(row_id: int, tenant_id: int, error: str, from_status=("sending",)) -> None:
    from app.services.system_errors import record_system_error

    with bypass_tenant_scope():
        db.session.query(BookingConfirmation).filter(
            BookingConfirmation.id == row_id, BookingConfirmation.status.in_(from_status)
        ).update({"status": "failed", "error": error[:120]}, synchronize_session=False)
        db.session.commit()
    record_system_error("mail", f"Buchungsbestaetigung: {error}"[:120], location="Buchungsbestätigung", tenant_id=tenant_id)


def _claim(row_id: int) -> bool:
    with bypass_tenant_scope():
        claimed = db.session.query(BookingConfirmation).filter(
            BookingConfirmation.id == row_id, BookingConfirmation.status == "pending"
        ).update({"status": "sending", "attempts": BookingConfirmation.attempts + 1}, synchronize_session=False)
        db.session.commit()
    return claimed == 1


def _office(tenant_id: int) -> dict:
    s = OfficeSettings.query.first()
    tenant = db.session.get(Tenant, tenant_id)
    name = (s.office_name if s and s.office_name else (tenant.name if tenant else "")) or ""
    return {"name": " ".join(name.split()), "email": ((s.email or "").strip() if s else ""),
            "phone": ((s.phone or "").strip() if s else ""), "address": " ".join(((s.address or "") if s else "").split())}


def _person(event: CalendarEvent) -> str | None:
    profile = getattr(event.assignee, "employee_profile", None) if getattr(event, "assignee", None) else None
    name = " ".join((getattr(profile, "display_name", "") or "").split())
    return name or None


def compose(row: BookingConfirmation, event: CalendarEvent, office: dict) -> tuple[str, str]:
    """Betreff und Klartext. Nur Termin- und Bueroangaben."""
    start, end = to_local(event.starts_at), to_local(event.ends_at)
    minutes = max(1, int((event.ends_at - event.starts_at).total_seconds() // 60))
    day = f"{WEEKDAYS[start.weekday()]}, {start.strftime('%d.%m.%Y')}"
    office_name = office["name"] or "Ihr Büro"
    lines = [f"Guten Tag {row.name}," if row.name else "Guten Tag,", "",
             "vielen Dank für Ihre Buchung. Ihr Termin ist bestätigt:", "",
             f"Termin:   {row.title}"]
    person = _person(event)
    if person:
        lines.append(f"Bei:      {person}")
    lines += [f"Datum:    {day}", f"Uhrzeit:  {start.strftime('%H:%M')} – {end.strftime('%H:%M')} Uhr",
              f"Dauer:    {minutes} Minuten"]
    kind = event.location_kind or ""
    if kind == "telefon":
        if row.phone:
            lines.append(f"Art:      Telefontermin – wir rufen Sie unter {row.phone} an.")
        else:
            lines.append("Art:      Telefontermin – bitte antworten Sie auf diese E-Mail mit Ihrer Telefonnummer.")
    elif kind == "video":
        lines.append("Art:      Videotermin – den Zugang erhalten Sie vom Büro.")
    elif event.location:
        lines.append(f"Ort:      {' '.join(event.location.split())}")
    elif office["address"]:
        lines.append(f"Ort:      {office['address']}")
    lines += [""]
    if office["email"]:
        reach = "antworten Sie einfach auf diese E-Mail"
        if office["phone"]:
            reach += f" oder rufen Sie uns an: {office['phone']}"
        lines.append(f"Bei Rückfragen oder wenn Sie den Termin nicht wahrnehmen können, {reach}.")
    elif office["phone"]:
        lines.append(f"Bei Rückfragen oder wenn Sie den Termin nicht wahrnehmen können, rufen Sie uns bitte an: {office['phone']}.")
    lines += ["", "Freundliche Grüße", office_name]
    for extra in (office["address"], office["phone"] and f"Telefon: {office['phone']}", office["email"] and f"E-Mail: {office['email']}"):
        if extra:
            lines.append(extra)
    subject = f"Terminbestätigung: {day}, {start.strftime('%H:%M')} Uhr – {office_name}"
    return subject, "\n".join(lines) + "\n"


def deliver(row_id: int) -> dict:
    """Celery-Aufgabe: genau ein Versuch je Einplanung, nur wenn die Zeile noch 'pending' ist."""
    from app.services import mailer

    with bypass_tenant_scope():
        row = db.session.get(BookingConfirmation, row_id)
        if row is None:
            return {"sent": False, "reason": "missing"}
        tenant_id = row.tenant_id
    if not _claim(row_id):
        return {"sent": False, "reason": "already_handled"}
    with use_tenant_id(tenant_id):
        row = db.session.get(BookingConfirmation, row_id)
        event = CalendarEvent.query.filter(CalendarEvent.id == row.event_id).first()
        if event is None or event.status != "scheduled":
            db.session.query(BookingConfirmation).filter(BookingConfirmation.id == row_id).update(
                {"status": "failed", "error": "Termin nicht mehr vorhanden"}, synchronize_session=False)
            db.session.commit()
            return {"sent": False, "reason": "event_gone"}
        office = _office(tenant_id)
        subject, body = compose(row, event, office)
        recipient = row.recipient
    if not mailer.is_mail_configured():
        _fail(row_id, tenant_id, "not_configured")
        return {"sent": False, "reason": "not_configured"}
    try:
        mailer.send_email(recipient, subject, body, reply_to=office["email"] or None, sender_name=office["name"] or None)
    except Exception as exc:
        code = getattr(exc, "smtp_code", None)
        # Keine SMTP-Antworttexte/Zugangsdaten speichern - nur Fehlerklasse und Statuscode.
        _fail(row_id, tenant_id, type(exc).__name__ + (f" (SMTP {code})" if code else ""))
        logger.warning("booking_confirmation.failed id=%s (%s)", row_id, type(exc).__name__)
        return {"sent": False, "reason": "smtp_error"}
    with bypass_tenant_scope():
        db.session.query(BookingConfirmation).filter(BookingConfirmation.id == row_id).update(
            {"status": "sent", "sent_at": utcnow_naive(), "error": None}, synchronize_session=False)
        db.session.commit()
    current_app.logger.info("booking_confirmation.sent id=%s event=%s", row_id, row.event_id)
    return {"sent": True}


def problems(limit: int = 20) -> list[dict]:
    """Fuer die Seite "Buchungslinks": nicht zugestellte Bestaetigungen (aktuelles Buero, letzte 30 Tage)."""
    since = utcnow_naive() - timedelta(days=30)
    rows = (BookingConfirmation.query.filter(BookingConfirmation.created_at >= since,
                                             BookingConfirmation.status.in_(("failed", "sending", "pending")))
            .order_by(BookingConfirmation.created_at.desc()).limit(limit * 3).all())
    out = []
    for row in rows:
        stuck = row.status in ("sending", "pending") and row.queued_at < utcnow_naive() - STUCK_AFTER
        if row.status != "failed" and not stuck:
            continue  # wird gerade gesendet
        event = CalendarEvent.query.filter(CalendarEvent.id == row.event_id).first()
        error = row.error if row.status == "failed" else "stuck"
        out.append({"id": row.id, "recipient": row.recipient, "name": row.name,
                    "when": to_local(event.starts_at).strftime("%d.%m.%Y %H:%M") if event else None,
                    "booked": to_local(row.created_at).strftime("%d.%m.%Y %H:%M"),
                    "error": error_label(error), "resendable": event is not None and event.status == "scheduled"})
        if len(out) >= limit:
            break
    return out


def resend(row_id: int) -> bool:
    """Buero-Admin: nicht zugestellte Bestaetigung erneut einplanen (aktuelles Buero)."""
    row = BookingConfirmation.query.filter(BookingConfirmation.id == row_id).first()
    if row is None:
        return False
    stuck = row.status in ("sending", "pending") and row.queued_at < utcnow_naive() - STUCK_AFTER
    if row.status != "failed" and not stuck:
        return False
    tenant_id = row.tenant_id
    updated = db.session.query(BookingConfirmation).filter(
        BookingConfirmation.id == row_id, BookingConfirmation.status == row.status
    ).update({"status": "pending", "error": None, "queued_at": utcnow_naive()}, synchronize_session=False)
    db.session.commit()
    if updated != 1:
        return False
    _enqueue(row_id, tenant_id)
    return True
