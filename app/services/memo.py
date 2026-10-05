"""Memo: Sprachnachricht -> Transkript. Nichts weiter.

Die Transkription laeuft lokal auf dem Server (faster-whisper, app/services/transcription_runner.py)
- ohne kostenpflichtige API und ohne dass Audio den Server verlaesst. Die Audiodatei liegt nur
fuer die Dauer der Transkription als temporaere Datei vor (TRANSCRIPTION_TMP_DIR) und wird danach
sofort geloescht; das Ergebnis wird unveraendert zurueckgegeben (keine Zusammenfassung,
Klassifizierung oder Folgeaktion)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

from flask import current_app

# Formate, die der lokale Decoder (PyAV/FFmpeg) liest. "webm"/"ogg"/"opus": Mikrofonaufnahmen
# aus Chrome/Edge/Firefox, "mp4"/"m4a"/"aac": Mikrofonaufnahmen aus Safari bzw. iPhone.
ALLOWED_AUDIO_EXTENSIONS = frozenset(
    {"mp3", "mp4", "mpeg", "mpga", "m4a", "aac", "wav", "webm", "ogg", "oga", "opus", "flac"}
)
MAX_AUDIO_BYTES = 25 * 1024 * 1024
# Vom Browser angegebene Typen ausser audio/*: Container, die Browser als Video melden
# (.mp4/.mpeg/.webm/.ogg), sowie "unbekannt" (z. B. .opus unter Windows). Alles andere (PDF, Text,
# Bilder, Programme) wird abgelehnt - massgeblich bleibt die Pruefung des Dateiinhalts.
ALLOWED_NON_AUDIO_MIME_TYPES = frozenset(
    {"video/mp4", "video/mpeg", "video/webm", "video/ogg", "application/ogg", "application/octet-stream", ""}
)
# Dateiendung -> erlaubte Container (erkannt am Dateianfang, siehe _audio_container).
CONTAINERS_BY_EXTENSION = {
    "mp3": {"mpeg"}, "mpeg": {"mpeg"}, "mpga": {"mpeg"},
    "aac": {"mpeg", "aac", "mp4"},
    "m4a": {"mp4"}, "mp4": {"mp4"},
    "wav": {"wav"},
    "ogg": {"ogg"}, "oga": {"ogg"}, "opus": {"ogg"},
    "webm": {"webm"},
    "flac": {"flac"},
}
# Erste Box einer MP4/M4A-Datei (ISO-BMFF): normalerweise "ftyp", bei aelteren Dateien auch andere.
_MP4_BOXES = (b"ftyp", b"moov", b"mdat", b"free", b"wide", b"skip")
RUNNER = Path(__file__).with_name("transcription_runner.py")
# Temporaere Audiodateien, die (z. B. nach einem Worker-Absturz) liegen geblieben sind.
STALE_TMP_SECONDS = 3600

# Fehlercodes des Runners -> verstaendliche Meldung.
RUNNER_ERRORS = {
    "decode": "Die Audiodatei konnte nicht gelesen werden. Ist es wirklich eine Audiodatei (z. B. MP3)? Bitte ggf. neu exportieren.",
    "empty": "In der Aufnahme wurde keine Sprache erkannt.",
    "memory": "Der Server hat gerade nicht genug Arbeitsspeicher. Bitte in einer Minute erneut versuchen.",
}


class MemoError(ValueError):
    """Fehler mit einer fuer den Nutzer verstaendlichen Meldung."""


def _audio_container(content: bytes) -> str | None:
    """Container anhand der Signatur am Dateianfang - unabhaengig von Name und angegebenem Typ."""
    head = content[:16]
    if head.startswith(b"ID3"):
        return "mpeg"
    if len(head) >= 2 and head[0] == 0xFF and head[1] & 0xE0 == 0xE0:
        return "mpeg"  # MPEG-Audio-Frame (MP3) bzw. ADTS (AAC)
    if head.startswith(b"ADIF"):
        return "aac"
    if head.startswith(b"RIFF") and head[8:12] == b"WAVE":
        return "wav"
    if head.startswith(b"OggS"):
        return "ogg"
    if head.startswith(b"\x1a\x45\xdf\xa3"):
        return "webm"
    if head.startswith(b"fLaC"):
        return "flac"
    if head[4:8] in _MP4_BOXES:
        return "mp4"
    return None


def validate_audio(filename: str | None, content: bytes, mimetype: str | None = None) -> str:
    """Prueft Dateiendung, den vom Browser angegebenen Typ und den tatsaechlichen Inhalt."""
    filename = os.path.basename(filename or "").strip()
    if not filename:
        raise MemoError("Bitte eine Audiodatei auswählen.")
    extension = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if extension not in ALLOWED_AUDIO_EXTENSIONS:
        raise MemoError("Dieses Dateiformat wird nicht unterstützt (erlaubt: MP3, M4A, WAV, OGG, OPUS, WEBM, MP4, AAC, FLAC).")
    if not content:
        raise MemoError("Die Datei ist leer.")
    if len(content) > MAX_AUDIO_BYTES:
        raise MemoError("Die Datei ist zu groß (maximal 25 MB).")
    mimetype = (mimetype or "").split(";")[0].strip().lower()
    if not (mimetype.startswith("audio/") or mimetype in ALLOWED_NON_AUDIO_MIME_TYPES):
        raise MemoError("Die Datei ist keine Audiodatei.")
    if _audio_container(content) not in CONTAINERS_BY_EXTENSION[extension]:
        raise MemoError(
            "Der Inhalt passt nicht zur Dateiendung oder ist keine unterstützte Audiodatei. "
            "Bitte die Originaldatei bzw. einen Export als MP3 oder M4A verwenden."
        )
    return filename


def max_audio_minutes() -> int:
    return int(current_app.config.get("TRANSCRIPTION_MAX_MINUTES") or 20)


def tmp_dir() -> Path:
    path = Path(current_app.config["TRANSCRIPTION_TMP_DIR"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def store_temp_audio(filename: str, content: bytes) -> str:
    """Legt die Audiodatei temporaer ab (nur fuer die Transkription) und raeumt dabei alte,
    liegen gebliebene Dateien weg. Liefert den Pfad."""
    directory = tmp_dir()
    now = time.time()
    for old in directory.glob("*.audio"):
        try:
            if now - old.stat().st_mtime > STALE_TMP_SECONDS:
                old.unlink()
        except OSError:
            pass
    path = directory / f"{uuid.uuid4().hex}.audio"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(content)
    return str(path)


def remove_temp_audio(path: str) -> None:
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass


def _runner_job(path: str) -> dict:
    config = current_app.config
    return {
        "path": path,
        "model": config["WHISPER_MODEL"],
        "model_dir": config["WHISPER_MODEL_DIR"],
        "compute_type": config["WHISPER_COMPUTE_TYPE"],
        "cpu_threads": config["WHISPER_CPU_THREADS"],
        "beam_size": config["WHISPER_BEAM_SIZE"],
        "language": "de",
        "max_seconds": max_audio_minutes() * 60,
        "lock_path": str(Path(config["TRANSCRIPTION_TMP_DIR"]) / "transcription.lock"),
    }


def transcribe_file(path: str) -> str:
    """Transkribiert die Audiodatei unter `path` lokal. Wirft MemoError mit verstaendlicher
    Meldung; technische Details landen nur im Server-Log (ohne Inhalte)."""
    timeout = int(current_app.config.get("TRANSCRIPTION_TIMEOUT_SECONDS") or 900)
    try:
        completed = subprocess.run(
            [sys.executable, str(RUNNER)],
            input=json.dumps(_runner_job(path)),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        current_app.logger.warning("memo.transcription.timeout seconds=%s", timeout)
        raise MemoError("Die Transkription hat zu lange gedauert. Bitte eine kürzere Aufnahme verwenden oder später erneut versuchen.") from exc

    lines = [line for line in completed.stdout.splitlines() if line.strip()]
    try:
        result = json.loads(lines[-1]) if lines else {}
    except ValueError:
        result = {}
    if "text" in result:
        current_app.logger.info(
            "memo.transcription.done audio_seconds=%s processing_seconds=%s", result.get("duration"), result.get("seconds")
        )
        text = (result["text"] or "").strip()
        if not text:
            raise MemoError(RUNNER_ERRORS["empty"])
        return text

    code = result.get("error")
    if code == "too_long":
        raise MemoError(f"Die Aufnahme ist zu lang (maximal {max_audio_minutes()} Minuten).")
    if code in RUNNER_ERRORS:
        raise MemoError(RUNNER_ERRORS[code])
    # Prozess abgestuerzt (z. B. vom Kernel wegen Speichermangel beendet) oder interner Fehler.
    current_app.logger.error(
        "memo.transcription.failed returncode=%s code=%s type=%s", completed.returncode, code, result.get("type")
    )
    if completed.returncode < 0:
        raise MemoError(RUNNER_ERRORS["memory"])
    raise MemoError("Die Transkription ist auf dem Server fehlgeschlagen. Bitte erneut versuchen – bleibt der Fehler, bitte den Administrator informieren.")


def transcribe_audio(filename: str, content: bytes) -> str:
    """Synchroner Weg (ohne JavaScript): temporaer ablegen, transkribieren, sofort loeschen."""
    path = store_temp_audio(filename, content)
    try:
        return transcribe_file(path)
    finally:
        remove_temp_audio(path)
