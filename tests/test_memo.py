"""Memo: Sprachnachricht hochladen -> Transkript anzeigen. Keine Speicherung, keine Folgeaktion."""

import io
import json
import struct
import wave
from pathlib import Path

import pytest

from app.models import MailboxCase

JSON = {"Accept": "application/json"}


@pytest.fixture()
def fake_transcription(monkeypatch):
    """Ersetzt nur die eigentliche Whisper-Transkription; Upload, temporaere Datei, Celery-Task
    (eager) und Antwort laufen echt. Aufgezeichnet wird der Inhalt der temporaeren Datei."""
    calls = []

    def fake(path):
        with open(path, "rb") as handle:
            calls.append(handle.read())
        return "Guten Tag, hier ist Herr Müller. Ich wollte mich bezüglich meines Vertrages melden."

    monkeypatch.setattr("app.services.memo.transcribe_file", fake)
    return calls


def _tmp_audio_files(app):
    return list(Path(app.config["TRANSCRIPTION_TMP_DIR"]).glob("*.audio"))


def _upload(client, filename="nachricht.mp3", content=b"ID3audio", headers=JSON, mimetype=None):
    file = (io.BytesIO(content), filename) if mimetype is None else (io.BytesIO(content), filename, mimetype)
    return client.post(
        "/sprachnachrichten/transkribieren",
        data={"file": file},
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
    assert body["token"]
    assert fake_transcription == [b"ID3audio"]


def test_upload_without_javascript_renders_transcript_page(auth_client, fake_transcription):
    resp = _upload(auth_client, filename="anruf.m4a", content=b"\x00\x00\x00\x1cftypM4A " + b"\x00" * 64, headers={})
    html = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert "Transkript" in html
    assert "hier ist Herr Müller" in html
    assert "anruf.m4a" in html
    assert ">Kopieren<" in html
    assert "Neue Sprachnachricht" in html


def test_upload_stores_nothing_and_triggers_nothing(app, auth_client, fake_transcription):
    _upload(auth_client)
    _upload(auth_client, headers={})
    assert MailboxCase.query.count() == 0
    # Die temporaere Audiodatei ist nach der Transkription wieder geloescht.
    assert _tmp_audio_files(app) == []


@pytest.mark.parametrize(
    ("filename", "content", "message"),
    [
        ("liste.pdf", b"%PDF", "Dateiformat"),
        ("leer.mp3", b"", "leer"),
        ("", b"abc", "Audiodatei auswählen"),
        # Umbenannte Dateien: Inhalt passt nicht zur Endung.
        ("rechnung.mp3", b"%PDF-1.7 ...", "passt nicht zur Dateiendung"),
        ("programm.m4a", b"MZ\x90\x00" + b"\x00" * 20, "passt nicht zur Dateiendung"),
        ("aufnahme.m4a", b"ID3audio", "passt nicht zur Dateiendung"),
        ("aufnahme.wav", b"OggS\x00\x02" + b"\x00" * 10, "passt nicht zur Dateiendung"),
    ],
)
def test_invalid_upload_is_rejected(auth_client, fake_transcription, filename, content, message):
    resp = _upload(auth_client, filename=filename, content=content)
    assert resp.status_code == 400
    assert message in resp.get_json()["error"]
    assert fake_transcription == []


def test_transcription_failure_returns_readable_error(app, auth_client, monkeypatch):
    def broken(path):
        raise RuntimeError("Whisper kaputt")

    monkeypatch.setattr("app.services.memo.transcribe_file", broken)
    resp = _upload(auth_client)
    assert resp.status_code == 502
    assert "fehlgeschlagen" in resp.get_json()["error"]
    assert "Whisper kaputt" not in resp.get_data(as_text=True)
    assert _tmp_audio_files(app) == []


def test_unreadable_audio_returns_specific_message(app, auth_client, monkeypatch):
    from app.services.memo import MemoError

    def unreadable(path):
        raise MemoError("Die Audiodatei konnte nicht gelesen werden.")

    monkeypatch.setattr("app.services.memo.transcribe_file", unreadable)
    resp = _upload(auth_client)
    assert resp.status_code == 422
    assert resp.get_json()["error"] == "Die Audiodatei konnte nicht gelesen werden."


# Dateianfang je Format, wie ihn echte Dateien bzw. Mikrofonaufnahmen haben.
SIGNATURES = {
    "mp3": b"ID3\x04\x00\x00\x00\x00", "mpeg": b"\xff\xfb\x90\x64\x00", "mpga": b"\xff\xf3\x40\xc4",
    "m4a": b"\x00\x00\x00\x1cftypM4A ", "mp4": b"\x00\x00\x00\x1cftypisom", "aac": b"\xff\xf1\x5c\x40",
    "wav": b"RIFF\x24\x00\x00\x00WAVEfmt ", "ogg": b"OggS\x00\x02", "oga": b"OggS\x00\x02",
    "opus": b"OggS\x00\x02", "webm": b"\x1a\x45\xdf\xa3\x9f", "flac": b"fLaC\x00\x00\x00\x22",
}


def test_every_allowed_format_has_a_signature_check():
    from app.services.memo import ALLOWED_AUDIO_EXTENSIONS, CONTAINERS_BY_EXTENSION

    assert set(CONTAINERS_BY_EXTENSION) == set(ALLOWED_AUDIO_EXTENSIONS) == set(SIGNATURES)


@pytest.mark.parametrize("extension", sorted(SIGNATURES))
def test_all_audio_formats_are_accepted(auth_client, fake_transcription, extension):
    content = SIGNATURES[extension] + b"\x00" * 64
    assert _upload(auth_client, filename=f"Aufnahme.{extension}", content=content).status_code == 200


def test_microphone_formats_are_accepted(auth_client, fake_transcription):
    # Chrome/Edge/Firefox nehmen WebM/Ogg (Opus) auf, Safari MP4/AAC - Browser melden dafuer
    # teils Video-Typen oder gar keinen Typ.
    for name, mimetype in (
        ("Aufnahme 2026-10-02 08-15.webm", "video/webm"), ("Aufnahme.ogg", "audio/ogg;codecs=opus"),
        ("Aufnahme.m4a", "audio/mp4"), ("Aufnahme.aac", "audio/aac"), ("Aufnahme.opus", "application/octet-stream"),
        ("Sprachmemo.m4a", "audio/x-m4a"), ("Video.mp4", "video/mp4"), ("Nachricht.mp3", ""),
    ):
        content = SIGNATURES[name.rsplit(".", 1)[1]] + b"\x00" * 64
        assert _upload(auth_client, filename=name, content=content, mimetype=mimetype).status_code == 200, name


@pytest.mark.parametrize("mimetype", ["application/pdf", "text/html", "image/jpeg", "application/x-msdownload"])
def test_non_audio_mime_type_is_rejected(auth_client, fake_transcription, mimetype):
    resp = _upload(auth_client, content=SIGNATURES["mp3"], mimetype=mimetype)
    assert resp.status_code == 400
    assert "keine Audiodatei" in resp.get_json()["error"]
    assert fake_transcription == []


def test_too_large_upload_is_rejected_with_json(app, auth_client, fake_transcription):
    resp = _upload(auth_client, content=SIGNATURES["mp3"] + b"\x00" * (25 * 1024 * 1024))
    assert resp.status_code == 413
    assert resp.headers.get("X-Zentriq-App") == "1"
    assert fake_transcription == []


class FakeResult:
    def __init__(self, task_id, value=None, ready=False, failed=False):
        self.id = task_id
        self.value = value
        self._ready = ready
        self._failed = failed
        self.forgotten = False

    def ready(self):
        return self._ready

    def failed(self):
        return self._failed

    def get(self, timeout=None, propagate=True):
        return self.value

    def forget(self):
        self.forgotten = True


def test_background_transcription_is_polled_by_the_starting_user_only(app, auth_client, employee_client, monkeypatch):
    """Ohne Eager-Modus: 202 + signierte Status-URL; das Ergebnis bekommt nur, wer es gestartet hat."""
    from app.tasks.memo_tasks import transcribe_memo

    started = []
    monkeypatch.setattr(transcribe_memo, "delay", lambda path: started.append(path) or FakeResult("job-1"))
    resp = _upload(auth_client)
    assert resp.status_code == 202
    status_url = resp.get_json()["status_url"]
    assert status_url.startswith("/sprachnachrichten/transkription/")
    assert len(started) == 1 and Path(started[0]).read_bytes() == b"ID3audio"

    results = {"job-1": FakeResult("job-1")}
    monkeypatch.setattr(app.extensions["celery"], "AsyncResult", lambda task_id: results[task_id])
    pending = auth_client.get(status_url, headers=JSON)
    assert pending.status_code == 202 and pending.get_json()["status"] == "pending"

    results["job-1"] = FakeResult("job-1", {"transcript": "Hallo aus dem Worker."}, ready=True)
    assert employee_client.get(status_url, headers=JSON).status_code == 404
    assert auth_client.get(status_url + "x", headers=JSON).status_code == 404
    done = auth_client.get(status_url, headers=JSON)
    assert done.status_code == 200
    body = done.get_json()
    assert body["transcript"] == "Hallo aus dem Worker." and body["filename"] == "nachricht.mp3" and body["token"]
    assert results["job-1"].forgotten  # Ergebnis wird aus dem Result-Backend entfernt

    results["job-1"] = FakeResult("job-1", {"error": "In der Aufnahme wurde keine Sprache erkannt."}, ready=True)
    failed = auth_client.get(status_url, headers=JSON)
    assert failed.status_code == 422 and "keine Sprache" in failed.get_json()["error"]


def test_unavailable_queue_gives_readable_message(app, auth_client, monkeypatch):
    from app.tasks.memo_tasks import transcribe_memo

    def down(path):
        raise ConnectionError("redis down")

    monkeypatch.setattr(transcribe_memo, "delay", down)
    resp = _upload(auth_client)
    assert resp.status_code == 422
    assert "nicht erreichbar" in resp.get_json()["error"]
    assert _tmp_audio_files(app) == []


def _wav(path: Path, seconds: float) -> None:
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(16000)
        handle.writeframes(struct.pack("<h", 0) * int(16000 * seconds))


def test_local_runner_reports_unreadable_and_empty_audio(app, tmp_path):
    """Echter Runner-Prozess (ohne Modell): Decoder-Fehler und leere Aufnahme werden erkannt."""
    from app.services import memo

    garbage = tmp_path / "kaputt.mp3"
    garbage.write_bytes(b"keine audiodaten" * 100)
    with pytest.raises(memo.MemoError, match="nicht gelesen"):
        memo.transcribe_file(str(garbage))

    short = tmp_path / "kurz.wav"
    _wav(short, 0.1)
    with pytest.raises(memo.MemoError, match="keine Sprache"):
        memo.transcribe_file(str(short))


def test_runner_decodes_to_16khz_mono(tmp_path):
    from app.services.transcription_runner import decode_audio

    path = tmp_path / "zwei.wav"
    _wav(path, 2.0)
    audio = decode_audio(str(path), max_seconds=60)
    assert audio.dtype.name == "float32" and abs(audio.shape[0] - 32000) < 400
    with pytest.raises(OverflowError):
        decode_audio(str(path), max_seconds=1)


def test_runner_job_uses_local_model_settings(app):
    from app.services import memo

    with app.test_request_context():
        job = memo._runner_job("/tmp/x.audio")
    assert job["model"] == app.config["WHISPER_MODEL"] == "small"
    assert job["compute_type"] == "int8" and job["language"] == "de"
    assert json.dumps(job)  # wird als JSON an den Runner uebergeben


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


def test_worker_log_never_contains_the_transcript(app):
    from celery.utils.saferepr import saferepr

    from app.tasks.memo_tasks import transcribe_memo

    logged = saferepr({"transcript": "Geheimer Inhalt der Sprachnachricht"}, transcribe_memo.resultrepr_maxsize)
    assert "Geheimer" not in logged


def test_expired_session_is_reported_as_json_not_redirect(client, fake_transcription):
    # memo.js meldet "Anmeldung abgelaufen" nur noch auf diese eindeutige Antwort hin.
    resp = _upload(client)
    assert resp.status_code == 401
    assert resp.get_json()["code"] == "login_required"
    assert resp.headers["X-Zentriq-App"] == "1"
    # Seitenaufrufe werden weiterhin zur Anmeldung umgeleitet.
    page = client.get("/sprachnachrichten")
    assert page.status_code == 302 and "/auth/login" in page.headers["Location"]


def test_csrf_failure_is_a_distinct_json_error(app, auth_client, fake_transcription):
    app.config["WTF_CSRF_ENABLED"] = True
    resp = _upload(auth_client)
    assert resp.status_code == 400
    assert resp.get_json()["code"] == "csrf"
    assert fake_transcription == []


def test_forbidden_json_request_gets_json_403(app, auth_client, monkeypatch):
    monkeypatch.setattr("app.auth.permissions.is_endpoint_allowed", lambda *a: False)
    resp = auth_client.get("/sprachnachrichten/transkription/x", headers=JSON)
    assert resp.status_code == 403
    assert resp.get_json()["code"] == "forbidden"


def test_foreign_upload_response_is_logged_without_content(app, employee_client, caplog):
    resp = employee_client.post(
        "/sprachnachrichten/diagnose",
        json={"status": 403, "redirected": True, "url": "https://www.zentriqai.de/blocked\nx", "server": "Proxy",
              "title": "Medientyp blockiert\n" + "x" * 300},
    )
    assert resp.status_code == 204
    line = next(r.getMessage() for r in caplog.records if "foreign_response" in r.getMessage())
    assert "status=403" in line and "server=Proxy" in line and "\n" not in line
    assert "title='Medientyp blockiert" in line and "x" * 151 not in line
