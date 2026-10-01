"""Memo: Sprachnachricht hochladen -> Transkript anzeigen. Keine Speicherung, keine Folgeaktion."""

import io

import pytest

from app.models import MailboxCase

JSON = {"Accept": "application/json"}


@pytest.fixture()
def fake_transcription(monkeypatch):
    calls = []

    def fake(filename, content):
        calls.append((filename, content))
        return "Guten Tag, hier ist Herr Müller. Ich wollte mich bezüglich meines Vertrages melden."

    monkeypatch.setattr("app.blueprints.dashboard.routes.transcribe_audio", fake)
    return calls


def _upload(client, filename="nachricht.mp3", content=b"ID3audio", headers=JSON):
    return client.post(
        "/sprachnachrichten/transkribieren",
        data={"file": (io.BytesIO(content), filename)},
        content_type="multipart/form-data",
        headers=headers,
    )


def test_memo_page_shows_only_upload(auth_client):
    html = auth_client.get("/sprachnachrichten").get_data(as_text=True)
    assert "Sprachnachricht hochladen" in html
    assert "Datei hier ablegen" in html
    assert "Sprachnachricht auswählen" in html
    assert 'type="file"' in html
    for removed in ("Postfach", "HUK", "Rückruf", "Schadenart", "Placetel"):
        assert removed not in html


def test_upload_returns_transcript_as_json(auth_client, fake_transcription):
    resp = _upload(auth_client)
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["transcript"].startswith("Guten Tag, hier ist Herr Müller.")
    assert body["filename"] == "nachricht.mp3"
    assert body["uploaded_at"]
    assert fake_transcription == [("nachricht.mp3", b"ID3audio")]


def test_upload_without_javascript_renders_transcript_page(auth_client, fake_transcription):
    resp = _upload(auth_client, filename="anruf.m4a", headers={})
    html = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert "Transkript" in html
    assert "hier ist Herr Müller" in html
    assert "anruf.m4a" in html
    assert ">Kopieren<" in html
    assert "Neue Sprachnachricht" in html


def test_upload_stores_nothing_and_triggers_nothing(auth_client, fake_transcription):
    _upload(auth_client)
    assert MailboxCase.query.count() == 0


@pytest.mark.parametrize(
    ("filename", "content", "message"),
    [
        ("liste.pdf", b"%PDF", "Dateiformat"),
        ("leer.mp3", b"", "leer"),
        ("", b"abc", "Audiodatei auswählen"),
    ],
)
def test_invalid_upload_is_rejected(auth_client, fake_transcription, filename, content, message):
    resp = _upload(auth_client, filename=filename, content=content)
    assert resp.status_code == 400
    assert message in resp.get_json()["error"]
    assert fake_transcription == []


def test_transcription_failure_returns_readable_error(auth_client, monkeypatch):
    def broken(filename, content):
        raise RuntimeError("API down")

    monkeypatch.setattr("app.blueprints.dashboard.routes.transcribe_audio", broken)
    resp = _upload(auth_client)
    assert resp.status_code == 502
    assert "fehlgeschlagen" in resp.get_json()["error"]
    assert "API down" not in resp.get_data(as_text=True)


def test_transcribe_audio_passes_file_to_transcription_api(app, monkeypatch):
    from app.services import memo

    captured = {}

    class FakeTranscriptions:
        def create(self, **kwargs):
            captured.update(kwargs)
            return type("Resp", (), {"text": "  Hallo Welt  "})()

    class FakeClient:
        audio = type("Audio", (), {"transcriptions": FakeTranscriptions()})()

    monkeypatch.setattr(memo, "get_openai_client", lambda **kwargs: FakeClient())
    with app.app_context():
        assert memo.transcribe_audio("a.wav", b"RIFF") == "Hallo Welt"
    assert captured["language"] == "de"
    assert captured["file"].name == "a.wav"
    assert captured["file"].read() == b"RIFF"


def test_employee_can_use_memo(employee_client, fake_transcription):
    # Memo steht allen Buero-Rollen offen (Hauptnavigation: Uebersicht, Leipziger Liste, Memo,
    # Zeiterfassung); der Kundenabgleich bleibt auf den eigenen Mandanten beschraenkt.
    assert employee_client.get("/sprachnachrichten").status_code == 200
    resp = _upload(employee_client)
    assert resp.status_code == 200
    assert resp.get_json()["transcript"].startswith("Guten Tag")
    assert len(fake_transcription) == 1


def test_anonymous_cannot_use_memo(client, fake_transcription):
    assert client.get("/sprachnachrichten").status_code in (302, 401)
    assert _upload(client).status_code in (302, 401)
    assert fake_transcription == []
