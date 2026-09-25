from email.message import EmailMessage

from app.services.mailbox.message import (
    attachment_sha256,
    build_source_key,
    message_matches_placetel,
    parse_mailbox_message,
)

CALLER_ID_HEADERS = "X-Caller-ID,X-Caller-Number,Caller-Number"


def build_raw_message(
    *,
    sender="mailbox@placetel.de",
    subject="Neue Mailbox-Nachricht",
    body="Ein Anrufer hat eine Nachricht hinterlassen.",
    message_id="<abc123@placetel.de>",
    caller_header=None,
    attach_mp3=True,
    content_type="audio/mpeg",
) -> bytes:
    message = EmailMessage()
    message["From"] = sender
    message["To"] = "posteingang@example.com"
    message["Subject"] = subject
    if message_id:
        message["Message-ID"] = message_id
    if caller_header:
        message["X-Caller-ID"] = caller_header
    message.set_content(body)
    if attach_mp3:
        message.add_attachment(
            b"fake-mp3-bytes",
            maintype="audio",
            subtype="mpeg",
            filename="voicemail.mp3",
        )
    return message.as_bytes()


def test_parse_mailbox_message_extracts_subject_sender_body_and_attachment():
    raw = build_raw_message()
    parsed = parse_mailbox_message(raw, CALLER_ID_HEADERS)

    assert parsed.sender == "mailbox@placetel.de"
    assert parsed.subject == "Neue Mailbox-Nachricht"
    assert "Anrufer" in parsed.body
    assert parsed.message_id == "<abc123@placetel.de>"
    assert len(parsed.attachments) == 1
    assert parsed.attachments[0].filename == "voicemail.mp3"
    assert parsed.attachments[0].content == b"fake-mp3-bytes"


def test_parse_mailbox_message_extracts_caller_phone_from_configured_header():
    raw = build_raw_message(caller_header="0521 1234567")
    parsed = parse_mailbox_message(raw, CALLER_ID_HEADERS)
    assert parsed.caller_phone == "+495211234567"


def test_parse_mailbox_message_without_attachment_has_empty_attachments():
    raw = build_raw_message(attach_mp3=False)
    parsed = parse_mailbox_message(raw, CALLER_ID_HEADERS)
    assert parsed.attachments == ()


def test_message_matches_placetel_requires_both_sender_and_subject_pattern():
    raw = build_raw_message(sender="voicemail@placetel.de", subject="Neue Sprachnachricht eingegangen")
    parsed = parse_mailbox_message(raw, CALLER_ID_HEADERS)
    assert message_matches_placetel(parsed, "placetel.de", "sprachnachricht") is True
    assert message_matches_placetel(parsed, "placetel.de", "rechnung") is False
    assert message_matches_placetel(parsed, "other-provider.de", "sprachnachricht") is False


def test_message_matches_placetel_fails_closed_without_configured_patterns():
    raw = build_raw_message(sender="voicemail@placetel.de", subject="Neue Sprachnachricht eingegangen")
    parsed = parse_mailbox_message(raw, CALLER_ID_HEADERS)
    assert message_matches_placetel(parsed, "", "") is False
    assert message_matches_placetel(parsed, "placetel.de", "") is False


def test_build_source_key_is_stable_for_same_message_id():
    raw1 = build_raw_message(message_id="<same-id@placetel.de>")
    raw2 = build_raw_message(message_id="<same-id@placetel.de>", body="anderer Text")
    assert build_source_key(raw1, "<same-id@placetel.de>") == build_source_key(raw2, "<same-id@placetel.de>")


def test_build_source_key_falls_back_to_raw_message_without_message_id():
    raw1 = build_raw_message(message_id=None)
    raw2 = build_raw_message(message_id=None)
    assert build_source_key(raw1, None) != build_source_key(raw2, None) or raw1 == raw2


def test_attachment_sha256_is_deterministic():
    raw = build_raw_message()
    parsed = parse_mailbox_message(raw, CALLER_ID_HEADERS)
    attachment = parsed.attachments[0]
    assert attachment_sha256(attachment) == attachment_sha256(attachment)
    assert len(attachment_sha256(attachment)) == 64
