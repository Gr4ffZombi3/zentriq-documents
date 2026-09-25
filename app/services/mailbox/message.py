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


# Von der OpenAI-Transkription akzeptierte Formate. Placetel verschickt Sprachnachrichten je
# nach Produkt/Einstellung als MP3 oder WAV; beides muss funktionieren.
AUDIO_EXTENSIONS = (".mp3", ".wav", ".m4a", ".mp4", ".mpeg", ".mpga", ".ogg", ".oga", ".webm", ".flac")
AUDIO_CONTENT_TYPES = {
    "audio/mpeg": ".mp3",
    "audio/mp3": ".mp3",
    "audio/mpeg3": ".mp3",
    "audio/x-mpeg-3": ".mp3",
    "audio/wav": ".wav",
    "audio/x-wav": ".wav",
    "audio/wave": ".wav",
    "audio/vnd.wave": ".wav",
    "audio/mp4": ".m4a",
    "audio/x-m4a": ".m4a",
    "audio/m4a": ".m4a",
    "audio/ogg": ".ogg",
    "audio/webm": ".webm",
    "audio/flac": ".flac",
    "audio/x-flac": ".flac",
}


def _audio_extension(filename: str | None, content_type: str) -> str | None:
    """Liefert die Dateiendung fuer einen unterstuetzten Audioanhang oder None. Die Endung des
    Dateinamens hat Vorrang (Mailer senden Audio oft als application/octet-stream)."""
    lowered = (filename or "").lower()
    for extension in AUDIO_EXTENSIONS:
        if lowered.endswith(extension):
            return extension
    return AUDIO_CONTENT_TYPES.get(content_type.lower())


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
        audio_extension = _audio_extension(part.get_filename(), part.get_content_type())
        if audio_extension is None:
            continue
        filename = part.get_filename() or f"mailbox-audio{audio_extension}"
        if not filename.lower().endswith(audio_extension):
            # Die Transkriptions-API erkennt das Format an der Dateiendung.
            filename = f"{filename}{audio_extension}"
        payload = part.get_payload(decode=True) or b""
        attachments.append(AudioAttachment(filename, part.get_content_type(), payload))
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
