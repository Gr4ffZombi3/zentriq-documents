"""Ende-zu-Ende-Absicherung der Sprachmemo-Automation ohne externe Systeme.

IMAP, OpenAI und der HUK-Browser werden durch Fakes ersetzt. Es wird nie ein echter
HUK-Rueckruf ausgeloest: Live-Einreichungen werden nur als geplanter Task-Aufruf protokolliert.
"""

import json
from datetime import datetime, timezone
from email.message import EmailMessage
from types import SimpleNamespace

import pytest

from app.extensions import db
from app.models import MailboxCallbackAttempt, MailboxCase, MailboxSyncCursor, Tenant
from app.models.enums import MailboxStatus
from app.services.mailbox import huk, imap, llm
from app.services.mailbox.imap import ImapMailboxReader, ImapMessage
from app.services.mailbox.pipeline import process_raw_message
from app.tasks import mailbox_tasks
from app.tasks.mailbox_tasks import poll_placetel_mailbox

TRANSCRIPT = (
    "Guten Tag, ich hatte heute einen Autounfall auf dem Parkplatz. "
    "Bitte rufen Sie mich zurück, meine Nummer ist 0521 1234567."
)
CLEAR_CLASSIFICATION = {
    "is_claim": True,
    "concern": "Schadenanliegen",
    "damage_type": "Kfz-Versicherung",
    "damage_confidence": 0.95,
    "callback_phone": "0521 1234567",
    "phone_confidence": 0.95,
    "reason": "Kfz-Schaden mit Rueckrufnummer.",
}


def build_placetel_mail(message_id="<vm-1@placetel.de>", *, sender="voicemail@placetel.de", audio=True):
    message = EmailMessage()
    message["From"] = sender
    message["Subject"] = "Neue Sprachnachricht von 05211234567"
    message["Message-ID"] = message_id
    message["Date"] = "Thu, 24 Sep 2026 10:15:00 +0200"
    message.set_content("Sie haben eine neue Sprachnachricht erhalten.")
    if audio:
        message.add_attachment(b"ID3-fake-mp3", maintype="audio", subtype="mpeg", filename="voicemail.mp3")
    return message.as_bytes()


class FakeOpenAI:
    """Minimaler Ersatz fuer den OpenAI-Client (Transkription + Chat-Completion)."""

    def __init__(self, transcript=TRANSCRIPT, classification=None):
        self.transcription_calls = []
        self.chat_calls = []
        content = json.dumps(classification if classification is not None else CLEAR_CLASSIFICATION)

        def transcribe(**kwargs):
            self.transcription_calls.append(kwargs)
            return SimpleNamespace(text=f"  {transcript}  ")

        def complete(**kwargs):
            self.chat_calls.append(kwargs)
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])

        self.audio = SimpleNamespace(transcriptions=SimpleNamespace(create=transcribe))
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=complete))


@pytest.fixture()
def fake_openai(monkeypatch):
    def install(**kwargs):
        client = FakeOpenAI(**kwargs)
        monkeypatch.setattr(llm, "get_openai_client", lambda **kwargs: client)
        return client

    return install


@pytest.fixture()
def placetel_config(app, tenant):
    app.config.update(
        PLACETEL_MAILBOX_ENABLED=True,
        PLACETEL_TENANT_ID=tenant.id,
        PLACETEL_SENDER_PATTERNS="placetel.de",
        PLACETEL_SUBJECT_PATTERNS="sprachnachricht",
        PLACETEL_IMAP_FOLDER="INBOX",
        MAILBOX_DRY_RUN=True,
        HUK_AUTOMATION_ENABLED=False,
    )
    return app.config


@pytest.fixture()
def fake_mailbox(monkeypatch):
    """Ersetzt den IMAP-Reader; liefert nur Nachrichten mit UID > last_uid."""
    state = {"messages": [], "calls": []}

    class FakeReader:
        def __init__(self, config):
            pass

        def fetch_since(self, last_uid):
            state["calls"].append(last_uid)
            start = int(last_uid) if last_uid else 0
            return [m for m in state["messages"] if int(m.uid) > start]

    monkeypatch.setattr(mailbox_tasks, "ImapMailboxReader", FakeReader)
    return state


@pytest.fixture()
def submit_calls(monkeypatch):
    """Protokolliert geplante HUK-Einreichungen, ohne sie auszufuehren."""
    calls = []
    monkeypatch.setattr(mailbox_tasks.submit_mailbox_case, "delay", lambda case_id: calls.append(case_id))
    return calls


# --- Postfach-Abruf ------------------------------------------------------------------------


def test_poll_processes_placetel_mail_end_to_end_in_dry_run(
    placetel_config, tenant, fake_mailbox, fake_openai, submit_calls
):
    client = fake_openai()
    fake_mailbox["messages"] = [
        ImapMessage("7", build_placetel_mail()),
        ImapMessage("8", build_placetel_mail("<newsletter@example.com>", sender="news@example.com")),
    ]

    result = poll_placetel_mailbox.delay().get()

    assert result == {"enabled": True, "checked": 2, "matched": 1, "created": 1}
    case = MailboxCase.query.one()
    assert case.tenant_id == tenant.id
    assert case.status == MailboxStatus.NEW
    assert case.dry_run is True
    assert case.transcript == TRANSCRIPT
    assert case.callback_phone == "+495211234567"
    assert case.phone_source == "transcript_explicit"
    assert case.concern == "Schadenanliegen"
    assert case.damage_type == "Kfz-Versicherung"
    assert case.audio_filename == "voicemail.mp3"
    assert case.source_uid == "7"
    assert case.received_at is not None

    # Transkription auf Deutsch, Klassifizierung mit JSON-Ausgabe und dem Transkript als Eingabe.
    assert client.transcription_calls[0]["language"] == "de"
    assert client.chat_calls[0]["response_format"] == {"type": "json_object"}
    assert client.chat_calls[0]["messages"][-1]["content"] == TRANSCRIPT

    cursor = MailboxSyncCursor.query.one()
    assert cursor.last_uid == "8"
    assert cursor.last_error is None

    # Dry Run: kein HUK-Aufruf eingeplant, kein Versuch angelegt.
    assert submit_calls == []
    assert MailboxCallbackAttempt.query.count() == 0


def test_poll_is_idempotent_and_resumes_after_cursor(placetel_config, fake_mailbox, fake_openai, submit_calls):
    fake_openai()
    fake_mailbox["messages"] = [ImapMessage("7", build_placetel_mail())]
    poll_placetel_mailbox.delay().get()

    # Gleiche Nachricht erneut (z. B. IMAP liefert bei "UID n:*" immer die letzte Nachricht).
    fake_mailbox["messages"].append(ImapMessage("9", build_placetel_mail()))
    second = poll_placetel_mailbox.delay().get()

    assert fake_mailbox["calls"] == [None, "7"]
    assert second["created"] == 0
    assert MailboxCase.query.count() == 1


def test_poll_schedules_huk_submission_only_when_live_mode_is_fully_enabled(
    placetel_config, fake_mailbox, fake_openai, submit_calls
):
    fake_openai()
    placetel_config.update(HUK_AUTOMATION_ENABLED=True, MAILBOX_DRY_RUN=True)
    fake_mailbox["messages"] = [ImapMessage("1", build_placetel_mail("<a@placetel.de>"))]
    poll_placetel_mailbox.delay().get()
    assert submit_calls == []

    placetel_config.update(HUK_AUTOMATION_ENABLED=False, MAILBOX_DRY_RUN=False)
    fake_mailbox["messages"].append(ImapMessage("2", build_placetel_mail("<b@placetel.de>")))
    poll_placetel_mailbox.delay().get()
    assert submit_calls == []

    placetel_config.update(HUK_AUTOMATION_ENABLED=True, MAILBOX_DRY_RUN=False)
    fake_mailbox["messages"].append(ImapMessage("3", build_placetel_mail("<c@placetel.de>")))
    poll_placetel_mailbox.delay().get()
    newest = MailboxCase.query.filter_by(source_uid="3").one()
    assert submit_calls == [newest.id]


def test_poll_does_not_schedule_review_cases_even_in_live_mode(
    placetel_config, fake_mailbox, fake_openai, submit_calls
):
    fake_openai(classification={**CLEAR_CLASSIFICATION, "damage_confidence": 0.3})
    placetel_config.update(HUK_AUTOMATION_ENABLED=True, MAILBOX_DRY_RUN=False)
    fake_mailbox["messages"] = [ImapMessage("1", build_placetel_mail())]
    poll_placetel_mailbox.delay().get()

    assert MailboxCase.query.one().status == MailboxStatus.REVIEW
    assert submit_calls == []


def test_poll_disabled_does_not_touch_mailbox(app, fake_mailbox):
    app.config["PLACETEL_MAILBOX_ENABLED"] = False
    assert poll_placetel_mailbox.delay().get()["enabled"] is False
    assert fake_mailbox["calls"] == []


def test_poll_records_imap_error_on_cursor(placetel_config, monkeypatch):
    class BrokenReader:
        def __init__(self, config):
            pass

        def fetch_since(self, last_uid):
            raise RuntimeError("IMAP nicht erreichbar")

    monkeypatch.setattr(mailbox_tasks, "ImapMailboxReader", BrokenReader)
    with pytest.raises(RuntimeError):
        poll_placetel_mailbox.delay().get()
    assert MailboxSyncCursor.query.one().last_error == "IMAP nicht erreichbar"


def test_beat_schedule_only_registered_when_mailbox_enabled(app):
    from app.celery_app import make_celery

    app.config["PLACETEL_MAILBOX_ENABLED"] = False
    assert "poll-placetel-mailbox" not in (make_celery(app).conf.beat_schedule or {})
    app.config["PLACETEL_MAILBOX_ENABLED"] = True
    schedule = make_celery(app).conf.beat_schedule
    assert schedule["poll-placetel-mailbox"]["task"] == "app.tasks.mailbox_tasks.poll_placetel_mailbox"


# --- IMAP: nur lesend ------------------------------------------------------------------------


def test_imap_reader_opens_folder_read_only_and_does_not_mark_seen(monkeypatch):
    log = []

    class FakeImap:
        def __init__(self, host, port):
            log.append(("connect", host, port))

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def login(self, user, password):
            log.append(("login", user))

        def select(self, folder, readonly=False):
            log.append(("select", folder, readonly))
            return "OK", [b"2"]

        def uid(self, command, *args):
            log.append(("uid", command, *args))
            if command == "search":
                return "OK", [b"5 6"]
            return "OK", [(b"5 (BODY[] {3}", b"raw"), b")"]

    monkeypatch.setattr(imap.imaplib, "IMAP4_SSL", FakeImap)
    reader = ImapMailboxReader(
        {
            "PLACETEL_IMAP_HOST": "imap.example.test",
            "PLACETEL_IMAP_PORT": 993,
            "PLACETEL_IMAP_USERNAME": "user",
            "PLACETEL_IMAP_PASSWORD": "secret",
            "PLACETEL_IMAP_FOLDER": "INBOX",
            "PLACETEL_IMAP_SSL": True,
        }
    )
    messages = reader.fetch_since("4")

    assert ("select", "INBOX", True) in log
    assert ("uid", "search", None, "UID 5:*") in log
    fetches = [entry for entry in log if entry[:2] == ("uid", "fetch")]
    assert fetches and all(entry[3] == "(BODY.PEEK[])" for entry in fetches)
    assert [m.uid for m in messages] == ["5", "6"]


def test_imap_reader_requires_complete_configuration():
    with pytest.raises(imap.MailboxConfigurationError):
        ImapMailboxReader({"PLACETEL_IMAP_HOST": "imap.example.test"})


# --- KI-Aufrufe ------------------------------------------------------------------------------


def test_transcribe_audio_uses_configured_model_and_german(app, fake_openai):
    client = fake_openai()
    app.config["OPENAI_TRANSCRIPTION_MODEL"] = "test-transcribe"
    assert llm.transcribe_audio("voicemail.mp3", "audio/mpeg", b"123") == TRANSCRIPT
    call = client.transcription_calls[0]
    assert call["model"] == "test-transcribe"
    assert call["language"] == "de"
    assert call["file"].name == "voicemail.mp3"


def test_classify_transcript_parses_valid_answer(app, fake_openai):
    fake_openai()
    result = llm.classify_transcript(TRANSCRIPT)
    assert result.is_claim is True
    assert result.damage_type == "Kfz-Versicherung"
    assert result.callback_phone == "0521 1234567"


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"damage_type": "Kfz"}, {"damage_type": None}),
        ({"damage_type": "kfz-versicherung"}, {"damage_type": "Kfz-Versicherung"}),
        ({"damage_type": ["Kfz-Versicherung"]}, {"damage_type": None}),
        ({"concern": "Schaden"}, {"concern": None}),
        ({"damage_confidence": 95}, {"damage_confidence": 0.0}),
        ({"damage_confidence": -0.2}, {"damage_confidence": 0.0}),
        ({"phone_confidence": "hoch"}, {"phone_confidence": 0.0}),
        ({"phone_confidence": float("nan")}, {"phone_confidence": 0.0}),
        ({"is_claim": "true"}, {"is_claim": False}),
        ({"callback_phone": 5211234567}, {"callback_phone": "5211234567"}),
        ({"reason": {"x": 1}}, {"reason": None}),
    ],
)
def test_classify_transcript_normalizes_off_schema_answers(app, fake_openai, overrides, expected):
    fake_openai(classification={**CLEAR_CLASSIFICATION, **overrides})
    result = llm.classify_transcript(TRANSCRIPT)
    for field, value in expected.items():
        assert getattr(result, field) == value


def test_classify_transcript_handles_unparseable_answer(app, monkeypatch):
    client = FakeOpenAI()
    client.chat.completions.create = lambda **kwargs: SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content="Das ist kein JSON"))]
    )
    monkeypatch.setattr(llm, "get_openai_client", lambda **kwargs: client)
    result = llm.classify_transcript(TRANSCRIPT)
    assert result.is_claim is False
    assert result.reason == llm.UNPARSEABLE_REASON


@pytest.mark.parametrize(
    "overrides",
    [{"damage_type": "Kfz"}, {"concern": "Schaden"}, {"damage_confidence": 95}, {"is_claim": None}],
)
def test_off_schema_answer_lands_in_review_not_failed(
    placetel_config, fake_mailbox, fake_openai, submit_calls, overrides
):
    fake_openai(classification={**CLEAR_CLASSIFICATION, **overrides})
    placetel_config.update(HUK_AUTOMATION_ENABLED=True, MAILBOX_DRY_RUN=False)
    fake_mailbox["messages"] = [ImapMessage("1", build_placetel_mail())]
    poll_placetel_mailbox.delay().get()

    case = MailboxCase.query.one()
    assert case.status == MailboxStatus.REVIEW
    assert case.transcript == TRANSCRIPT
    assert case.review_reason
    assert submit_calls == []


def test_transcript_is_kept_when_classification_fails(placetel_config, tenant):
    def failing_classifier(_transcript):
        raise ValueError("KI-Antwort ungueltig")

    result = process_raw_message(
        tenant.id,
        build_placetel_mail(),
        transcriber=lambda *_: TRANSCRIPT,
        classifier=failing_classifier,
    )
    assert result.mailbox_case.status == MailboxStatus.FAILED
    assert result.mailbox_case.transcript == TRANSCRIPT
    db.session.expire_all()
    assert MailboxCase.query.one().transcript == TRANSCRIPT


# --- C4: KI-Rufnummer nur, wenn sie im Transkript belegbar ist ---------------------------------

HALLUCINATED = {**CLEAR_CLASSIFICATION, "callback_phone": "0170 9998887", "phone_confidence": 0.99}
NO_NUMBER_TRANSCRIPT = "Guten Tag, ich hatte einen Autounfall. Bitte melden Sie sich bei mir."


def _process(tenant_id, transcript, classification, **mail_kwargs):
    from app.services.mailbox.schemas import MailboxClassification

    return process_raw_message(
        tenant_id,
        build_placetel_mail(**mail_kwargs),
        transcriber=lambda *_: transcript,
        classifier=lambda _t: MailboxClassification.model_validate(classification),
    ).mailbox_case


def test_hallucinated_phone_falls_back_to_caller_id(placetel_config, tenant):
    case = _process(tenant.id, NO_NUMBER_TRANSCRIPT, HALLUCINATED)
    assert case.callback_phone == "+495211234567"  # Anrufer-ID aus dem Betreff
    assert case.phone_source == "email_caller_id"
    assert case.events[0].details["llm_phone_unverified"] is True


def test_hallucinated_phone_without_caller_id_goes_to_review(placetel_config, tenant):
    placetel_config["PLACETEL_CALLER_ID_HEADERS"] = "X-Caller-ID"
    raw = build_placetel_mail()
    raw = raw.replace(b"Neue Sprachnachricht von 05211234567", b"Neue Sprachnachricht")
    from app.services.mailbox.schemas import MailboxClassification

    case = process_raw_message(
        tenant.id,
        raw,
        transcriber=lambda *_: NO_NUMBER_TRANSCRIPT,
        classifier=lambda _t: MailboxClassification.model_validate(HALLUCINATED),
    ).mailbox_case
    assert case.callback_phone is None
    assert case.status == MailboxStatus.REVIEW
    assert "Rückrufnummer" in case.review_reason


def test_hallucinated_phone_is_never_submitted_live(placetel_config, fake_mailbox, fake_openai, submit_calls):
    fake_openai(transcript=NO_NUMBER_TRANSCRIPT, classification=HALLUCINATED)
    placetel_config.update(HUK_AUTOMATION_ENABLED=True, MAILBOX_DRY_RUN=False)
    fake_mailbox["messages"] = [ImapMessage("1", build_placetel_mail())]
    poll_placetel_mailbox.delay().get()
    case = MailboxCase.query.one()
    assert case.callback_phone != "+491709998887"


@pytest.mark.parametrize(
    "spoken",
    ["0170 9998887", "0170/999 88 87", "+49 170 9998887", "+49 (0) 170-9998887", "0049 170 9998887"],
)
def test_llm_phone_accepted_when_present_in_transcript(placetel_config, tenant, spoken):
    transcript = f"Ich hatte einen Autounfall. Sie erreichen die Werkstatt unter {spoken}."
    case = _process(tenant.id, transcript, HALLUCINATED)
    assert case.callback_phone == "+491709998887"
    assert case.phone_source == "transcript_explicit"


def test_phone_appears_in_text_does_not_join_separate_numbers():
    from app.services.mailbox.phone import phone_appears_in_text

    assert phone_appears_in_text("+491709998887", "Vertrag 0170 und Schaden 9998887") is False
    assert phone_appears_in_text("+491709998887", "Nummer 01709998887") is True
    assert phone_appears_in_text("+491709998887", "Nummer 101709998887") is False
    assert phone_appears_in_text(None, "0170 9998887") is False


# --- Audioformate --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("filename", "maintype", "subtype", "expected_name"),
    [
        ("voicemail.mp3", "audio", "mpeg", "voicemail.mp3"),
        ("voicemail.wav", "audio", "x-wav", "voicemail.wav"),
        ("voicemail.WAV", "application", "octet-stream", "voicemail.WAV"),
        ("nachricht", "audio", "wav", "nachricht.wav"),
        (None, "audio", "mpeg", "mailbox-audio.mp3"),
        ("voicemail.m4a", "audio", "mp4", "voicemail.m4a"),
        ("voicemail.ogg", "audio", "ogg", "voicemail.ogg"),
    ],
)
def test_supported_audio_attachments_are_detected(filename, maintype, subtype, expected_name):
    from app.services.mailbox.message import parse_mailbox_message

    message = EmailMessage()
    message["From"] = "voicemail@placetel.de"
    message["Subject"] = "Neue Sprachnachricht"
    message.set_content("Text")
    message.add_attachment(b"audio-bytes", maintype=maintype, subtype=subtype, filename=filename)
    if filename is None:
        message.get_payload()[-1].replace_header("Content-Disposition", "attachment")

    parsed = parse_mailbox_message(message.as_bytes(), "")
    assert [a.filename for a in parsed.attachments] == [expected_name]
    assert parsed.attachments[0].content == b"audio-bytes"


def test_non_audio_attachments_are_ignored():
    from app.services.mailbox.message import parse_mailbox_message

    message = EmailMessage()
    message["From"] = "voicemail@placetel.de"
    message["Subject"] = "Neue Sprachnachricht"
    message.set_content("Text")
    message.add_attachment(b"%PDF", maintype="application", subtype="pdf", filename="rechnung.pdf")
    message.add_attachment(b"GIF", maintype="image", subtype="gif", filename="logo.gif")
    assert parse_mailbox_message(message.as_bytes(), "").attachments == ()


def test_wav_voicemail_is_processed_end_to_end(placetel_config, tenant, fake_openai):
    client = fake_openai()
    message = EmailMessage()
    message["From"] = "voicemail@placetel.de"
    message["Subject"] = "Neue Sprachnachricht von 05211234567"
    message["Message-ID"] = "<wav@placetel.de>"
    message.set_content("Neue Nachricht")
    message.add_attachment(b"RIFF....WAVEfmt ", maintype="audio", subtype="x-wav", filename="voicemail.wav")

    case = process_raw_message(tenant.id, message.as_bytes()).mailbox_case
    assert case.status == MailboxStatus.NEW
    assert case.audio_filename == "voicemail.wav"
    assert client.transcription_calls[0]["file"].name == "voicemail.wav"


# --- OPENAI_BASE_URL: Mailbox darf die Leipziger-Liste nicht umlenken ----------------------------


def test_mailbox_base_url_does_not_affect_leipziger_extraction_client(app):
    from app.services.llm.client import get_openai_client

    app.config.update(OPENAI_API_KEY="sk-test", OPENAI_BASE_URL=None, MAILBOX_OPENAI_BASE_URL="https://mailbox.example/v1")
    assert "api.openai.com" in str(get_openai_client().base_url)
    assert str(llm._mailbox_openai_client().base_url).startswith("https://mailbox.example/v1")


def test_global_openai_base_url_still_applies_to_all_clients(app):
    from app.services.llm.client import get_openai_client

    app.config.update(OPENAI_API_KEY="sk-test", OPENAI_BASE_URL="https://proxy.example/v1", MAILBOX_OPENAI_BASE_URL=None)
    assert str(get_openai_client().base_url).startswith("https://proxy.example/v1")
    assert str(llm._mailbox_openai_client().base_url).startswith("https://proxy.example/v1")


def test_leipziger_extraction_uses_general_client(monkeypatch):
    """Die Leipziger-Extraktion importiert den allgemeinen Client - nicht den Mailbox-Client."""
    import inspect

    from app.services.llm import extraction

    source = inspect.getsource(extraction)
    assert "from app.services.llm.client import get_openai_client" in source
    assert "MAILBOX_OPENAI_BASE_URL" not in source


# --- HUK-Formular: Dry Run klickt nie auf "Rueckruf ausfuehren" ------------------------------


class FakeLocator:
    def __init__(self, page, role, name):
        self.page, self.role, self.name = page, role, name

    def fill(self, value):
        self.page.actions.append(("fill", self.name, value))

    def check(self):
        self.page.actions.append(("check", self.name))

    def click(self):
        self.page.actions.append(("click", self.role, self.name))

    def is_visible(self):
        return False

    def filter(self, **kwargs):
        return self

    def count(self):
        return 1 if self.page.captcha and "captcha" in self.name else 0


class FakePage:
    def __init__(self, captcha=False):
        self.actions = []
        self.captcha = captcha
        self.url = "about:blank"

    def goto(self, url, **kwargs):
        self.url = url
        self.actions.append(("goto", url))

    def get_by_role(self, role, name, exact=False):
        return FakeLocator(self, role, name)

    def locator(self, selector):
        return FakeLocator(self, "css", selector)

    def get_by_text(self, *args, **kwargs):  # pragma: no cover - nur im Live-Pfad
        raise AssertionError("Bestaetigungstext darf im Dry Run nicht abgefragt werden.")


def test_fill_huk_form_dry_run_fills_but_never_submits(app):
    page = FakePage()
    form_data = huk.build_huk_form_data("+495211234567", "Kfz-Versicherung")

    result = huk.fill_huk_form(page, form_data, submit=False)

    assert result["prepared"] is True
    assert result["submitted"] is False
    assert ("goto", app.config["HUK_FORM_URL"]) in page.actions
    assert ("fill", "Vorwahl", "0521") in page.actions
    assert ("fill", "Rufnummer", "1234567") in page.actions
    assert ("check", "Schadenanliegen") in page.actions
    assert ("click", "menuitem", "Kfz-Versicherung") in page.actions
    assert not any(action[-1] == "Rückruf ausführen" for action in page.actions)


def test_fill_huk_form_aborts_on_captcha(app):
    page = FakePage(captcha=True)
    form_data = huk.build_huk_form_data("+495211234567", "Kfz-Versicherung")
    with pytest.raises(huk.CaptchaDetectedError):
        huk.fill_huk_form(page, form_data, submit=False)
    assert not any(action[0] == "fill" for action in page.actions)


@pytest.mark.parametrize("damage_type", [dt for dt in huk.HUK_DAMAGE_TYPES if dt != "Sonstiges"])
def test_all_automatable_damage_types_build_form_data(damage_type):
    assert huk.build_huk_form_data("+4915112345678", damage_type)["damage_type"] == damage_type


def test_mobile_number_is_split_for_huk_form_without_losing_digits():
    # libphonenumber trennt z. B. "01511 2345678"; entscheidend ist die vollstaendige Nummer.
    data = huk.build_huk_form_data("+4915112345678", "Haftpflichtversicherung")
    assert data["area_code"].startswith("015")
    assert data["area_code"] + data["subscriber_number"] == "015112345678"


# --- Mandantentrennung -----------------------------------------------------------------------


def _foreign_case():
    from app.tenancy import bypass_tenant_scope

    with bypass_tenant_scope():
        other = Tenant(name="Fremder Mandant", slug="fremd")
        db.session.add(other)
        db.session.flush()
        case = MailboxCase(
            tenant_id=other.id,
            source_key="foreign",
            source_subject="Fremde Sprachnachricht",
            received_at=datetime(2026, 9, 20, tzinfo=timezone.utc),
            status=MailboxStatus.NEW,
        )
        db.session.add(case)
        db.session.commit()
        return case.id


def test_mailbox_case_of_other_tenant_is_not_visible(auth_client):
    case_id = _foreign_case()
    assert auth_client.get(f"/mailbox/{case_id}").status_code == 404
    assert auth_client.post(f"/mailbox/{case_id}/retry").status_code == 404
    assert "Fremde Sprachnachricht" not in auth_client.get("/").get_data(as_text=True)
