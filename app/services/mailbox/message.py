import hashlib
import html
import re
from dataclasses import dataclass
from datetime import timezone
from email import policy
from email.message import Message
from email.parser import BytesParser
from email.utils import parsedate_to_datetime

from app.services.mailbox.phone import extract_phone_candidates


@dataclass(frozen=True)
class AudioAttachment:
    filename: str
    content_type: str
    content: bytes


@dataclass(frozen=True)
class ParsedMailboxMessage:
    message_id: str | None
    sender: str
    subject: str
    received_at: object | None
    body: str
    caller_phone: str | None
    attachments: tuple[AudioAttachment, ...]


def csv_values(value: str | None) -> tuple[str, ...]:
    return tuple(item.strip().lower() for item in (value or "").split(",") if item.strip())


def parse_mailbox_message(raw_message: bytes, caller_id_headers: str) -> ParsedMailboxMessage:
    message = BytesParser(policy=policy.default).parsebytes(raw_message)
    subject = str(message.get("Subject", ""))
    sender = str(message.get("From", ""))
    body = _extract_body(message)
    caller_phone = _extract_caller_phone(message, subject, body, caller_id_headers)
    attachments: list[AudioAttachment] = []
    for part in message.iter_attachments():
        filename = part.get_filename() or "mailbox-audio"
        content_type = part.get_content_type()
        if filename.lower().endswith(".mp3") or content_type in {"audio/mpeg", "audio/mp3"}:
            payload = part.get_payload(decode=True) or b""
            attachments.append(AudioAttachment(filename, content_type, payload))
    return ParsedMailboxMessage(
        message_id=str(message.get("Message-ID")) if message.get("Message-ID") else None,
        sender=sender,
        subject=subject,
        received_at=_parse_received_at(message.get("Date")),
        body=body,
        caller_phone=caller_phone,
        attachments=tuple(attachments),
    )


def message_matches_placetel(parsed: ParsedMailboxMessage, sender_patterns: str, subject_patterns: str) -> bool:
    senders = csv_values(sender_patterns)
    subjects = csv_values(subject_patterns)
    if not senders or not subjects:
        return False
    sender = parsed.sender.lower()
    subject = parsed.subject.lower()
    return any(pattern in sender for pattern in senders) and any(pattern in subject for pattern in subjects)


def build_source_key(raw_message: bytes, message_id: str | None) -> str:
    identity = message_id.strip().lower().encode("utf-8") if message_id else raw_message
    return hashlib.sha256(identity).hexdigest()


def attachment_sha256(attachment: AudioAttachment) -> str:
    return hashlib.sha256(attachment.content).hexdigest()


def _extract_body(message: Message) -> str:
    try:
        body = message.get_body(preferencelist=("plain", "html"))
        if body is None:
            return ""
        content = body.get_content()
        if body.get_content_type() == "text/html":
            content = re.sub(r"<[^>]+>", " ", content)
            content = html.unescape(content)
        return re.sub(r"\s+", " ", content).strip()
    except (AttributeError, LookupError, UnicodeError):
        return ""


def _extract_caller_phone(message: Message, subject: str, body: str, caller_id_headers: str) -> str | None:
    for header in csv_values(caller_id_headers):
        actual_name = next((name for name in message.keys() if name.lower() == header), None)
        if actual_name:
            candidates = extract_phone_candidates(str(message.get(actual_name)))
            if candidates:
                return candidates[0]
    candidates = extract_phone_candidates(f"{subject}\n{body}")
    return candidates[0] if candidates else None


def _parse_received_at(value: str | None):
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)
