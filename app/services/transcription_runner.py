"""Lokale Transkription (faster-whisper) als eigener, kurzlebiger Prozess.

Wird von app/services/memo.py per Dateipfad gestartet (bewusst NICHT als `python -m app...`,
damit weder Flask noch die App geladen werden). Das Whisper-Modell belegt je nach Groesse
mehrere hundert MB Arbeitsspeicher; als eigener Prozess wird dieser Speicher nach jeder
Transkription vollstaendig freigegeben, und ein Absturz (z. B. Speichermangel) trifft nie den
Celery-Worker oder die Web-App.

Eingabe: JSON auf stdin (siehe memo._runner_job). Ausgabe: genau eine JSON-Zeile auf stdout:
{"text": "...", "duration": 12.3} oder {"error": "<code>"} - nie Audio- oder Textinhalte in
Fehlermeldungen. Eine Dateisperre sorgt dafuer, dass auf dem Server immer nur eine
Transkription gleichzeitig laeuft (begrenzter Arbeitsspeicher)."""

import fcntl
import json
import os
import sys
import time

SAMPLE_RATE = 16000


def decode_audio(path: str, max_seconds: float):
    """Decodiert eine beliebige Audiodatei (MP3, M4A/AAC, WAV, OGG/Opus, WEBM, FLAC ...) mit
    PyAV zu 16 kHz Mono float32. Eigene Implementierung statt faster_whisper.decode_audio:
    unabhaengig von PyAV-API-Aenderungen und mit Laengengrenze waehrend des Decodierens."""
    import av
    import numpy as np

    chunks = []
    total = 0
    limit = int(max_seconds * SAMPLE_RATE)
    resampler = av.AudioResampler(format="flt", layout="mono", rate=SAMPLE_RATE)
    with av.open(path, mode="r") as container:
        if not container.streams.audio:
            raise ValueError("no_audio_stream")
        stream = container.streams.audio[0]
        for frame in container.decode(stream):
            for resampled in resampler.resample(frame):
                data = resampled.to_ndarray().reshape(-1)
                chunks.append(data)
                total += data.shape[0]
            if total > limit:
                raise OverflowError("too_long")
        for resampled in resampler.resample(None):
            data = resampled.to_ndarray().reshape(-1)
            chunks.append(data)
            total += data.shape[0]
    if not chunks:
        return np.zeros(0, dtype=np.float32)
    return np.concatenate(chunks).astype(np.float32, copy=False)


def _emit(payload: dict) -> int:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()
    return 0


def run(job: dict) -> int:
    lock_path = job["lock_path"]
    os.makedirs(os.path.dirname(lock_path), exist_ok=True)
    with open(lock_path, "a+") as lock:
        # Wartet, bis eine laufende Transkription fertig ist (Zeitlimit setzt der Aufrufer).
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            audio = decode_audio(job["path"], float(job["max_seconds"]))
        except OverflowError:
            return _emit({"error": "too_long"})
        except Exception:  # noqa: BLE001 - jede Decoder-Ausnahme heisst: Datei nicht lesbar
            return _emit({"error": "decode"})
        duration = audio.shape[0] / SAMPLE_RATE
        if duration < 0.3:
            return _emit({"error": "empty"})

        from faster_whisper import WhisperModel

        started = time.monotonic()
        settings = {
            "device": "cpu",
            "compute_type": job["compute_type"],
            "cpu_threads": int(job["cpu_threads"]),
            "download_root": job["model_dir"],
        }
        try:
            # Normalfall: Modell liegt bereits vor (flask whisper-download) - keine Netzabfrage.
            model = WhisperModel(job["model"], local_files_only=True, **settings)
        except Exception:  # noqa: BLE001 - Modell fehlt noch: einmalig herunterladen
            model = WhisperModel(job["model"], **settings)
        segments, _info = model.transcribe(
            audio,
            language=job.get("language") or "de",
            beam_size=int(job["beam_size"]),
            vad_filter=True,
            condition_on_previous_text=False,
        )
        text = " ".join(segment.text.strip() for segment in segments).strip()
        return _emit({"text": text, "duration": round(duration, 1), "seconds": round(time.monotonic() - started, 1)})


def main() -> int:
    try:
        job = json.loads(sys.stdin.read())
    except ValueError:
        return _emit({"error": "bad_job"})
    try:
        return run(job)
    except MemoryError:
        return _emit({"error": "memory"})
    except Exception as exc:  # noqa: BLE001 - nur die Fehlerklasse, nie Inhalte
        return _emit({"error": "internal", "type": type(exc).__name__})


if __name__ == "__main__":
    sys.exit(main())
