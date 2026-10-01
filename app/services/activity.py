"""Aktivitaetsprotokoll des Bueros: lesbare Darstellung ausgewaehlter Audit-Ereignisse.

Gezeigt wird nur, WER WANN WAS ausgeloest hat - nie fachliche Inhalte (Memo-Texte,
Kundendaten, Listeninhalte). Die Ereignisse selbst enthalten diese Inhalte auch nicht."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

from sqlalchemy.orm import selectinload

from app.models import AuditLog, User
from app.models.audit_log import AuditEventType
from app.services.timetracking.clock import local_today, to_local

_TEXTS = {
    AuditEventType.USER_CREATED: "Benutzer {target} angelegt",
    AuditEventType.USER_UPDATED: "Benutzer {target} bearbeitet",
    AuditEventType.USER_DELETED: "Benutzer {target} gelöscht",
    AuditEventType.PASSWORD_CHANGED: "Eigenes Passwort geändert",
    AuditEventType.PASSWORD_RESET_TRIGGERED: "Passwort-Reset für {target} ausgelöst",
    AuditEventType.TWO_FACTOR_ENABLED: "Zwei-Faktor-Authentifizierung eingerichtet",
    AuditEventType.TWO_FACTOR_RESET: "Zwei-Faktor-Authentifizierung von {target} zurückgesetzt",
    AuditEventType.TIME_CLOCK_IN: "Zeiterfassung gestartet",
    AuditEventType.TIME_CLOCK_OUT: "Zeiterfassung beendet",
    AuditEventType.TIME_CORRECTED: "Arbeitszeit von {target} korrigiert",
    AuditEventType.TIME_CORRECTION_REQUESTED: "Korrekturantrag gestellt",
    AuditEventType.TIME_CORRECTION_DECIDED: "Korrekturantrag entschieden",
    AuditEventType.MEMO_TRANSCRIBED: "Memo transkribiert",
    AuditEventType.LEIPZIGER_LIST_UPLOADED: "Leipziger Liste hochgeladen",
    AuditEventType.SESSIONS_REVOKED: "Andere Sitzungen abgemeldet",
    AuditEventType.CUSTOMER_MERGED: "Kundendatensätze zusammengeführt",
    AuditEventType.MEMO_ASSIGNED: "Memo einem Kunden zugeordnet",
    AuditEventType.CUSTOMER_CREATED: "Kunde aus Memo angelegt",
    AuditEventType.ASSISTANT_USED: "KI-Assistent verwendet",
}


@dataclass(frozen=True)
class Activity:
    at: datetime
    day: date
    actor: str
    text: str


def _name(user: User | None, fallback: str | None) -> str:
    if user is not None:
        profile = user.employee_profile
        if profile is not None and profile.display_name:
            return profile.display_name
        return user.email
    return fallback or "System"


def describe_activities(entries: list[AuditLog]) -> list[Activity]:
    """Ein Benutzer-Lookup fuer alle Akteure und Zielpersonen (eigener Mandant)."""
    user_ids = set()
    for entry in entries:
        if entry.actor_user_id:
            user_ids.add(entry.actor_user_id)
        target = (entry.details or {}).get("target_user_id") or (entry.details or {}).get("employee_user_id")
        if isinstance(target, int):
            user_ids.add(target)
    users = (
        {user.id: user for user in User.query.options(selectinload(User.employee_profile)).filter(User.id.in_(user_ids))}
        if user_ids
        else {}
    )
    result = []
    for entry in entries:
        details = entry.details or {}
        target_id = details.get("target_user_id") or details.get("employee_user_id")
        target = _name(users.get(target_id), details.get("email") or "–")
        template = _TEXTS.get(entry.event_type, entry.event_type.value)
        local = to_local(entry.created_at)
        result.append(
            Activity(
                at=local,
                day=local.date(),
                actor=_name(users.get(entry.actor_user_id), entry.actor_email_snapshot),
                text=template.format(target=target),
            )
        )
    return result


def day_label(day: date) -> str:
    today = local_today()
    if day == today:
        return "Heute"
    if (today - day).days == 1:
        return "Gestern"
    return day.strftime("%d.%m.%Y")
