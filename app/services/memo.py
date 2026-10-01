"""Memo: Sprachnachricht -> Transkript. Nichts weiter.

Die Audiodatei wird direkt an die Transkription uebergeben und nicht gespeichert; das Ergebnis
wird unveraendert zurueckgegeben (keine Zusammenfassung, Klassifizierung oder Folgeaktion)."""

import io
import os

from flask import current_app

from app.services.llm.client import get_openai_client

# Von der OpenAI-Transkription unterstuetzte Formate.
ALLOWED_AUDIO_EXTENSIONS = frozenset({"mp3", "mp4", "mpeg", "mpga", "m4a", "wav", "webm", "ogg", "oga", "flac"})
# Obergrenze der Transkriptions-API.
MAX_AUDIO_BYTES = 25 * 1024 * 1024
# Bleibt unter dem nginx-Standard-Timeout (60 s), damit der Nutzer eine verstaendliche Meldung bekommt.
TRANSCRIPTION_TIMEOUT_SECONDS = 55


class MemoError(ValueError):
    """Fehler mit einer fuer den Nutzer verstaendlichen Meldung."""


def validate_audio(filename: str | None, content: bytes) -> str:
    filename = os.path.basename(filename or "").strip()
    if not filename:
        raise MemoError("Bitte eine Audiodatei auswählen.")
    extension = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if extension not in ALLOWED_AUDIO_EXTENSIONS:
        raise MemoError("Dieses Dateiformat wird nicht unterstützt (erlaubt: MP3, M4A, WAV, OGG, WEBM, MP4, FLAC).")
    if not content:
        raise MemoError("Die Datei ist leer.")
    if len(content) > MAX_AUDIO_BYTES:
        raise MemoError("Die Datei ist zu groß (maximal 25 MB).")
    return filename


def transcribe_audio(filename: str, content: bytes) -> str:
    audio = io.BytesIO(content)
    audio.name = filename
    client = get_openai_client(base_url=current_app.config.get("MAILBOX_OPENAI_BASE_URL"))
    response = client.audio.transcriptions.create(
        model=current_app.config["OPENAI_TRANSCRIPTION_MODEL"],
        file=audio,
        language="de",
        response_format="json",
        timeout=TRANSCRIPTION_TIMEOUT_SECONDS,
    )
    return (response.text or "").strip()
