from dataclasses import dataclass
from datetime import datetime, timezone

from flask import current_app
from sqlalchemy.exc import IntegrityError

from app.extensions import db
from app.models import MailboxCase, MailboxCaseEvent
from app.models.enums import MailboxStatus
from app.services.mailbox.llm import classify_transcript, transcribe_audio
from app.services.mailbox.message import (
    attachment_sha256,
    build_source_key,
    message_matches_placetel,
    parse_mailbox_message,
)
from app.services.mailbox.phone import extract_explicit_callback_phone, normalize_callback_phone
from app.services.mailbox.schemas import HUK_DAMAGE_TYPES, MailboxClassification


@dataclass(frozen=True)
class ProcessingResult:
    mailbox_case: MailboxCase | None
    created: bool
    matched: bool


def add_case_event(mailbox_case: MailboxCase, event_type: str, details=None, actor_user_id=None):
    event = MailboxCaseEvent(
        tenant_id=mailbox_case.tenant_id,
        mailbox_case=mailbox_case,
        event_type=event_type,
        details=details,
        actor_user_id=actor_user_id,
    )
    db.session.add(event)
    return event


def process_raw_message(
    tenant_id: int,
    raw_message: bytes,
    *,
    source_uid: str | None = None,
    source_mailbox: str | None = None,
    transcriber=transcribe_audio,
    classifier=classify_transcript,
) -> ProcessingResult:
    parsed = parse_mailbox_message(raw_message, current_app.config["PLACETEL_CALLER_ID_HEADERS"])
    if not message_matches_placetel(
        parsed,
        current_app.config["PLACETEL_SENDER_PATTERNS"],
        current_app.config["PLACETEL_SUBJECT_PATTERNS"],
    ):
        return ProcessingResult(None, created=False, matched=False)

    source_key = build_source_key(raw_message, parsed.message_id)
    existing = MailboxCase.query.filter_by(source_key=source_key).first()
    if existing is not None:
        return ProcessingResult(existing, created=False, matched=True)

    attachment = parsed.attachments[0] if parsed.attachments else None
    mailbox_case = MailboxCase(
        tenant_id=tenant_id,
        source_key=source_key,
        source_message_id=parsed.message_id,
        source_uid=source_uid,
        source_mailbox=source_mailbox,
        source_sender=parsed.sender,
        source_subject=parsed.subject,
        received_at=parsed.received_at,
        caller_phone=normalize_callback_phone(parsed.caller_phone),
        audio_filename=attachment.filename if attachment else None,
        audio_content_type=attachment.content_type if attachment else None,
        audio_sha256=attachment_sha256(attachment) if attachment else None,
        audio_size_bytes=len(attachment.content) if attachment else None,
        processing_started_at=datetime.now(timezone.utc),
        dry_run=bool(current_app.config["MAILBOX_DRY_RUN"]),
    )
    db.session.add(mailbox_case)
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        existing = MailboxCase.query.filter_by(source_key=source_key).first()
        return ProcessingResult(existing, created=False, matched=True)

    try:
        if attachment is None:
            raise ValueError("Keine MP3-Audiodatei in der Placetel-Nachricht gefunden.")
        max_bytes = int(current_app.config["MAILBOX_AUDIO_MAX_MB"]) * 1024 * 1024
        if not attachment.content or len(attachment.content) > max_bytes:
            raise ValueError("Der Audioanhang ist leer oder überschreitet die konfigurierte Größenbegrenzung.")

        transcript = transcriber(attachment.filename, attachment.content_type, attachment.content)
        if not transcript.strip():
            raise ValueError("Die Transkription hat keinen Text geliefert.")
        classification: MailboxClassification = classifier(transcript)
        _apply_classification(mailbox_case, transcript, classification)
        add_case_event(
            mailbox_case,
            "processed",
            {
                "status": mailbox_case.status.value,
                "dry_run": mailbox_case.dry_run,
                "phone_source": mailbox_case.phone_source,
                "damage_type": mailbox_case.damage_type,
            },
        )
    except Exception as exc:
        mailbox_case.status = MailboxStatus.FAILED
        mailbox_case.last_error = str(exc)
        mailbox_case.review_reason = "Die Mailbox-Nachricht konnte nicht vollständig verarbeitet werden."
        add_case_event(mailbox_case, "processing_failed", {"error": str(exc)})
        current_app.logger.exception(
            "mailbox.processing.failed tenant_id=%s mailbox_case_id=%s",
            tenant_id,
            mailbox_case.id,
        )
    finally:
        mailbox_case.processing_started_at = None
        mailbox_case.processed_at = datetime.now(timezone.utc)
        db.session.commit()

    return ProcessingResult(mailbox_case, created=True, matched=True)


def _apply_classification(
    mailbox_case: MailboxCase,
    transcript: str,
    classification: MailboxClassification,
) -> None:
    explicit_phone = extract_explicit_callback_phone(transcript)
    classified_phone = normalize_callback_phone(classification.callback_phone)
    caller_phone = normalize_callback_phone(mailbox_case.caller_phone)
    if explicit_phone:
        callback_phone = explicit_phone
        phone_source = "transcript_explicit"
        phone_confidence = 1.0
    elif classified_phone:
        callback_phone = classified_phone
        phone_source = "transcript_explicit"
        phone_confidence = classification.phone_confidence
    else:
        callback_phone = caller_phone
        phone_source = "email_caller_id" if caller_phone else None
        phone_confidence = 0.9 if caller_phone else 0.0

    damage_type = classification.damage_type if classification.damage_type in HUK_DAMAGE_TYPES else None
    mailbox_case.transcript = transcript.strip()
    mailbox_case.callback_phone = callback_phone
    mailbox_case.phone_source = phone_source
    mailbox_case.phone_confidence = phone_confidence
    mailbox_case.concern = "Schadenanliegen" if classification.is_claim else None
    mailbox_case.damage_type = damage_type
    mailbox_case.damage_confidence = classification.damage_confidence
    mailbox_case.classification_reason = classification.reason
    mailbox_case.last_error = None

    review_reasons: list[str] = []
    if not classification.is_claim or classification.concern != "Schadenanliegen":
        review_reasons.append("Kein eindeutiges Schadenanliegen erkannt.")
    if not callback_phone:
        review_reasons.append("Keine geeignete deutsche Rückrufnummer erkannt.")
    elif phone_confidence < float(current_app.config["MAILBOX_MIN_PHONE_CONFIDENCE"]):
        review_reasons.append("Die Rückrufnummer ist nicht sicher genug erkannt.")
    if not damage_type or damage_type == "Sonstiges":
        review_reasons.append("Die Schadenart ist nicht eindeutig zuordenbar.")
    elif classification.damage_confidence < float(current_app.config["MAILBOX_MIN_DAMAGE_CONFIDENCE"]):
        review_reasons.append("Die Schadenart ist nicht sicher genug erkannt.")

    mailbox_case.review_reason = " ".join(review_reasons) or None
    mailbox_case.status = MailboxStatus.REVIEW if review_reasons else MailboxStatus.NEW
