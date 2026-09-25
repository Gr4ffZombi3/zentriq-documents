from email.message import EmailMessage

from app.models import MailboxCase
from app.models.enums import MailboxStatus
from app.services.mailbox.pipeline import process_raw_message
from app.services.mailbox.schemas import MailboxClassification


def build_raw_message(*, sender="mailbox@placetel.de", subject="Neue Sprachnachricht", message_id="<x@placetel.de>", attach=True):
    message = EmailMessage()
    message["From"] = sender
    message["Subject"] = subject
    message["Message-ID"] = message_id
    message.set_content("Ein Anrufer hat eine Nachricht hinterlassen.")
    if attach:
        message.add_attachment(b"fake-mp3-bytes", maintype="audio", subtype="mpeg", filename="voicemail.mp3")
    return message.as_bytes()


def configure_placetel(app):
    app.config["PLACETEL_SENDER_PATTERNS"] = "placetel.de"
    app.config["PLACETEL_SUBJECT_PATTERNS"] = "sprachnachricht"


def clear_transcript(_filename, _content_type, _content):
    return "Ich hatte einen Autounfall und brauche einen Rückruf. Meine Nummer ist 0521 1234567."


def clear_classification(_transcript):
    return MailboxClassification(
        is_claim=True,
        concern="Schadenanliegen",
        damage_type="Kfz-Versicherung",
        damage_confidence=0.95,
        callback_phone="+495211234567",
        phone_confidence=0.95,
        reason="Eindeutiger Kfz-Schaden mit genannter Rückrufnummer.",
    )


def ambiguous_transcript(_filename, _content_type, _content):
    return "Ich hatte einen Schaden und wäre über einen Rückruf dankbar."


def uncertain_classification(_transcript):
    return MailboxClassification(
        is_claim=True,
        concern="Schadenanliegen",
        damage_type="Kfz-Versicherung",
        damage_confidence=0.4,
        callback_phone=None,
        phone_confidence=0.0,
        reason="Unklare Angaben.",
    )


def not_a_claim_classification(_transcript):
    return MailboxClassification(is_claim=False, reason="Allgemeine Vertragsfrage, kein Schaden.")


def test_process_raw_message_ignores_non_placetel_messages(app, tenant):
    configure_placetel(app)
    with app.app_context():
        raw = build_raw_message(sender="someone-else@example.com")
        result = process_raw_message(tenant.id, raw, transcriber=clear_transcript, classifier=clear_classification)

    assert result.matched is False
    assert result.created is False
    assert result.mailbox_case is None
    assert MailboxCase.query.count() == 0


def test_process_raw_message_creates_case_with_status_new_when_unambiguous(app, tenant):
    configure_placetel(app)
    with app.app_context():
        raw = build_raw_message()
        result = process_raw_message(tenant.id, raw, transcriber=clear_transcript, classifier=clear_classification)

        assert result.created is True
        case = result.mailbox_case
        assert case is not None
        assert case.status == MailboxStatus.NEW
        assert case.callback_phone == "+495211234567"
        assert case.damage_type == "Kfz-Versicherung"
        assert case.concern == "Schadenanliegen"
        assert case.review_reason is None
        assert case.transcript.startswith("Ich hatte einen Autounfall")


def test_process_raw_message_is_idempotent_for_same_source_key(app, tenant):
    configure_placetel(app)
    with app.app_context():
        raw = build_raw_message()
        first = process_raw_message(tenant.id, raw, transcriber=clear_transcript, classifier=clear_classification)
        second = process_raw_message(tenant.id, raw, transcriber=clear_transcript, classifier=clear_classification)

    assert first.created is True
    assert second.created is False
    assert second.mailbox_case.id == first.mailbox_case.id
    assert MailboxCase.query.count() == 1


def test_process_raw_message_flags_low_confidence_for_review(app, tenant):
    configure_placetel(app)
    with app.app_context():
        raw = build_raw_message()
        result = process_raw_message(tenant.id, raw, transcriber=ambiguous_transcript, classifier=uncertain_classification)

        case = result.mailbox_case
        assert case.status == MailboxStatus.REVIEW
        assert case.review_reason
        assert case.callback_phone is None


def test_process_raw_message_flags_non_claim_for_review(app, tenant):
    configure_placetel(app)
    with app.app_context():
        raw = build_raw_message()
        result = process_raw_message(tenant.id, raw, transcriber=clear_transcript, classifier=not_a_claim_classification)

        case = result.mailbox_case
        assert case.status == MailboxStatus.REVIEW
        assert "Schadenanliegen" in case.review_reason


def test_process_raw_message_marks_failed_without_audio_attachment(app, tenant):
    configure_placetel(app)
    with app.app_context():
        raw = build_raw_message(attach=False)
        result = process_raw_message(tenant.id, raw, transcriber=clear_transcript, classifier=clear_classification)

        case = result.mailbox_case
        assert case.status == MailboxStatus.FAILED
        assert case.last_error


def test_process_raw_message_prefers_explicit_transcript_phone_over_caller_id(app, tenant):
    configure_placetel(app)

    def classification_without_phone(_transcript):
        return MailboxClassification(
            is_claim=True,
            concern="Schadenanliegen",
            damage_type="Kfz-Versicherung",
            damage_confidence=0.95,
            callback_phone=None,
            phone_confidence=0.0,
            reason="Schaden erkannt.",
        )

    with app.app_context():
        message = EmailMessage()
        message["From"] = "mailbox@placetel.de"
        message["Subject"] = "Neue Sprachnachricht"
        message["Message-ID"] = "<explicit-phone@placetel.de>"
        message["X-Caller-ID"] = "0170 9999999"
        message.set_content("Bitte rufen Sie mich zurück unter 0521 1234567.")
        message.add_attachment(b"fake-mp3-bytes", maintype="audio", subtype="mpeg", filename="voicemail.mp3")

        result = process_raw_message(
            tenant.id,
            message.as_bytes(),
            transcriber=lambda *_: "Bitte rufen Sie mich zurück unter 0521 1234567.",
            classifier=classification_without_phone,
        )

        case = result.mailbox_case
        assert case.callback_phone == "+495211234567"
        assert case.phone_source == "transcript_explicit"
