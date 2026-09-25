"""Darstellung der Sprachnachrichten fuer Bueroanwender.

Reine Anzeige-Logik: leitet aus vorhandenen MailboxCase-/MailboxCallbackAttempt-Daten
verstaendliche Bezeichnungen ab. Liest nur, veraendert nichts und liegt bewusst ausserhalb
von app/services/mailbox/, damit die Automation (Placetel -> Audio -> Transkription ->
Klassifizierung -> Telefonnummer -> Schadenart -> HUK) unberuehrt bleibt."""

from dataclasses import dataclass

from app.models import CallbackAttemptStatus, MailboxStatus

STATUS_FILTERS = (
    ("all", "Alle"),
    (MailboxStatus.NEW.value, "In Bearbeitung"),
    (MailboxStatus.REVIEW.value, "Prüfung nötig"),
    (MailboxStatus.CALLBACK_REQUESTED.value, "Erledigt"),
    (MailboxStatus.FAILED.value, "Fehler"),
)

PHONE_SOURCE_LABELS = {
    "transcript_explicit": "Nachricht",
    "email_caller_id": "Anruferkennung",
    "manual": "manuelle Korrektur",
}


@dataclass(frozen=True)
class VoiceMessageView:
    status_label: str
    status_tone: str
    needs_review: bool
    callback_label: str
    callback_tone: str
    callback_at: object
    hint: str | None
    hint_is_error: bool
    phone_source_label: str | None


def _callback_state(case) -> tuple[str, str, object]:
    attempts = list(case.attempts or [])
    submitted = [a for a in attempts if a.status == CallbackAttemptStatus.SUBMITTED and not a.dry_run]
    if submitted:
        return "Übermittelt", "success", submitted[0].completed_at or submitted[0].started_at
    prepared = [
        a for a in attempts
        if a.status == CallbackAttemptStatus.PREPARED or (a.status == CallbackAttemptStatus.SUBMITTED and a.dry_run)
    ]
    if prepared:
        return "Vorbereitet (Testbetrieb)", "info", prepared[0].completed_at or prepared[0].started_at
    if any(a.status == CallbackAttemptStatus.FAILED for a in attempts):
        return "Nicht übermittelt", "danger", None
    if case.status == MailboxStatus.CALLBACK_REQUESTED:
        # Ohne gespeicherten Versuch (z. B. Altdaten) gibt der Vorgang selbst Auskunft.
        if case.dry_run:
            return "Vorbereitet (Testbetrieb)", "info", case.submitted_at
        return "Übermittelt", "success", case.submitted_at
    return "Noch nicht vorbereitet", "neutral", None


def build_voice_message_view(case) -> VoiceMessageView:
    callback_label, callback_tone, callback_at = _callback_state(case)
    status = case.status
    if status == MailboxStatus.CALLBACK_REQUESTED:
        status_label, status_tone = "Erledigt", "success"
    elif status == MailboxStatus.REVIEW:
        status_label, status_tone = "Manuelle Prüfung nötig", "warning"
    elif status == MailboxStatus.FAILED:
        status_label, status_tone = "Fehler", "danger"
    else:
        status_label, status_tone = "In Bearbeitung", "info"

    if status == MailboxStatus.FAILED:
        hint, hint_is_error = case.last_error or "Die Übermittlung ist fehlgeschlagen.", True
    elif status == MailboxStatus.REVIEW:
        hint, hint_is_error = case.review_reason or "Bitte Rufnummer und Schadenart prüfen.", False
    elif status == MailboxStatus.NEW:
        hint, hint_is_error = ("Wird verarbeitet" if not case.transcript else "Wartet auf Übermittlung"), False
    else:
        hint, hint_is_error = None, False

    return VoiceMessageView(
        status_label=status_label,
        status_tone=status_tone,
        needs_review=status == MailboxStatus.REVIEW,
        callback_label=callback_label,
        callback_tone=callback_tone,
        callback_at=callback_at,
        hint=hint,
        hint_is_error=hint_is_error,
        phone_source_label=PHONE_SOURCE_LABELS.get(case.phone_source) if case.phone_source else None,
    )
