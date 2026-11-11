"""Zentriq-Rechner: holt KI-Auftraege bei Zentriq ab und bearbeitet sie mit einem lokalen Modell.

Laeuft auf dem eigenen Windows-Laptop (oder Linux/macOS). Nur Python-Standardbibliothek - keine
Installation weiterer Pakete noetig. Die Verbindung geht immer vom Rechner aus (HTTPS zu Zentriq,
HTTP nur zum lokalen Modell auf 127.0.0.1) - zu Hause muessen keine Ports freigegeben werden.

Aufruf:
  python zentriq_rechner.py              Dauerbetrieb (Strg+C beendet)
  python zentriq_rechner.py --pruefen    Verbindung zu Zentriq und zum lokalen Modell pruefen
  python zentriq_rechner.py --einmal     hoechstens einen Auftrag bearbeiten und beenden
  python zentriq_rechner.py --config C:\\Pfad\\zentriq-rechner.ini

Konfiguration: zentriq-rechner.ini neben diesem Skript (Vorlage: zentriq-rechner.ini.beispiel).
Der Zugangsschluessel kann statt in der Datei auch in der Umgebungsvariable ZENTRIQ_RECHNER_ZUGANG stehen.

Das Protokoll enthaelt nur Auftragsnummer, Art, Dauer und Fehlerart - nie Dokumentinhalte,
Antworten oder den Zugangsschluessel."""

from __future__ import annotations

import argparse
import configparser
import json
import logging
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

VERSION = "1.0"
ALL_KINDS = ("kfz_policy_extract", "assistant_chat")
log = logging.getLogger("zentriq-rechner")


class ModelError(Exception):
    """Fehler beim lokalen Modell. retryable: voruebergehend (z. B. Modell laedt noch)."""

    def __init__(self, message: str, retryable: bool = False):
        super().__init__(message)
        self.retryable = retryable


# --- Konfiguration ----------------------------------------------------------------------------------


class Settings:
    def __init__(self, path: Path):
        cfg = configparser.ConfigParser()
        if path.is_file():
            cfg.read(path, encoding="utf-8")
        self.url = cfg.get("zentriq", "adresse", fallback="").rstrip("/")
        self.token = os.environ.get("ZENTRIQ_RECHNER_ZUGANG") or cfg.get("zentriq", "zugang", fallback="")
        self.backend = cfg.get("modell", "schnittstelle", fallback="ollama").strip().lower()
        self.model_url = cfg.get("modell", "adresse", fallback="http://127.0.0.1:11434").rstrip("/")
        self.model = cfg.get("modell", "modell", fallback="").strip()
        self.text_model = cfg.get("modell", "textmodell", fallback="").strip() or self.model
        self.model_timeout = cfg.getint("modell", "zeitlimit_sekunden", fallback=900)
        self.context = cfg.getint("modell", "kontext_tokens", fallback=8192)
        self.images = cfg.getboolean("modell", "bilder", fallback=True)
        self.poll = cfg.getint("betrieb", "abfrage_sekunden", fallback=5)
        kinds = cfg.get("betrieb", "arten", fallback=",".join(ALL_KINDS))
        self.kinds = [k.strip() for k in kinds.split(",") if k.strip() in ALL_KINDS]
        self.logfile = cfg.get("betrieb", "protokoll", fallback="").strip()

    def problems(self) -> list[str]:
        out = []
        if not self.url.startswith("https://") and not self.url.startswith("http://127.0.0.1") and not self.url.startswith("http://localhost"):
            out.append("[zentriq] adresse fehlt oder ist nicht https://")
        if not self.token.startswith("zqw_"):
            out.append("[zentriq] zugang fehlt (Schluessel beginnt mit zqw_)")
        if self.backend not in ("ollama", "openai", "test"):
            out.append("[modell] schnittstelle muss ollama, openai oder test sein")
        if self.backend != "test" and not self.model:
            out.append("[modell] modell fehlt (z. B. der Name aus 'ollama list')")
        if not self.kinds:
            out.append("[betrieb] arten enthaelt keine bekannte Auftragsart")
        return out

    @property
    def model_label(self) -> str:
        return "Test-Worker (kein Modell)" if self.backend == "test" else f"{self.model} ({self.backend})"


# --- HTTP -------------------------------------------------------------------------------------------


def _request(url: str, body: dict | None, headers: dict | None = None, timeout: float = 30) -> tuple[int, dict | None]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method="POST" if body is not None else "GET",
                                 headers={"Content-Type": "application/json", "User-Agent": f"zentriq-rechner/{VERSION}", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            return resp.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read() or b"null")
        except ValueError:
            return exc.code, None


class Zentriq:
    def __init__(self, settings: Settings):
        self.s = settings

    def call(self, path: str, body: dict | None = None, timeout: float = 30) -> tuple[int, dict | None]:
        payload = {"modell": self.s.model_label, "version": VERSION, **(body or {})}
        return _request(f"{self.s.url}/api/rechner{path}", payload, {"Authorization": f"Bearer {self.s.token}"}, timeout)


# --- Lokales Modell ---------------------------------------------------------------------------------


def _ollama(s: Settings, system: str, messages: list[dict], images: list[str], fmt: str | None, max_tokens: int) -> str:
    msgs = [{"role": "system", "content": system}] if system else []
    msgs += [{"role": m["role"], "content": m["content"]} for m in messages]
    if images and msgs:
        msgs[-1]["images"] = images
    body = {"model": s.model if images else s.text_model, "messages": msgs, "stream": False,
            "options": {"temperature": 0, "num_predict": max_tokens, "num_ctx": s.context}}
    if fmt == "json":
        body["format"] = "json"
    try:
        status, data = _request(f"{s.model_url}/api/chat", body, timeout=s.model_timeout)
    except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
        raise ModelError("Lokales Modell nicht erreichbar", retryable=True) from exc
    if status != 200 or not isinstance(data, dict):
        raise ModelError(f"Lokales Modell meldet HTTP {status}", retryable=status >= 500)
    return ((data.get("message") or {}).get("content") or "").strip()


def _openai(s: Settings, system: str, messages: list[dict], images: list[str], fmt: str | None, max_tokens: int) -> str:
    msgs = [{"role": "system", "content": system}] if system else []
    for i, m in enumerate(messages):
        if images and i == len(messages) - 1:
            content = [{"type": "text", "text": m["content"]}] + [
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}} for b64 in images]
            msgs.append({"role": m["role"], "content": content})
        else:
            msgs.append({"role": m["role"], "content": m["content"]})
    body = {"model": s.model if images else s.text_model, "messages": msgs, "temperature": 0, "max_tokens": max_tokens}
    if fmt == "json":
        body["response_format"] = {"type": "json_object"}
    try:
        status, data = _request(f"{s.model_url}/v1/chat/completions", body, timeout=s.model_timeout)
    except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
        raise ModelError("Lokales Modell nicht erreichbar", retryable=True) from exc
    if status != 200 or not isinstance(data, dict) or not data.get("choices"):
        raise ModelError(f"Lokales Modell meldet HTTP {status}", retryable=status >= 500)
    return (data["choices"][0].get("message") or {}).get("content", "").strip()


def _test(kind: str) -> str:
    """Test-Worker: prueft nur den Ablauf mit Zentriq, ohne Modell."""
    if kind == "kfz_policy_extract":
        return json.dumps({"versicherungsschein": {}, "beitragsrechnung": {}})
    return "Testantwort vom Test-Worker (kein lokales Modell angeschlossen)."


def run_model(s: Settings, kind: str, task: dict) -> str:
    if s.backend == "test":
        return _test(kind)
    system = str(task.get("system") or "")
    if "messages" in task:
        messages = [m for m in task["messages"] if isinstance(m, dict) and m.get("role") in ("user", "assistant")]
    else:
        messages = [{"role": "user", "content": str(task.get("prompt") or "")}]
    images = [str(b) for b in (task.get("images") or [])] if s.images else []
    fmt = task.get("format") if task.get("format") == "json" else None
    max_tokens = int(task.get("max_tokens") or 1200)
    backend = _ollama if s.backend == "ollama" else _openai
    return backend(s, system, messages, images, fmt, max_tokens)


def model_ready(s: Settings) -> tuple[bool, str]:
    if s.backend == "test":
        return True, "Test-Worker (kein Modell)"
    try:
        if s.backend == "ollama":
            status, data = _request(f"{s.model_url}/api/tags", None, timeout=5)
            names = [m.get("name", "") for m in (data or {}).get("models", [])] if status == 200 else []
        else:
            status, data = _request(f"{s.model_url}/v1/models", None, timeout=5)
            names = [m.get("id", "") for m in (data or {}).get("data", [])] if status == 200 else []
    except (urllib.error.URLError, TimeoutError, ConnectionError):
        return False, f"Lokales Modell unter {s.model_url} nicht erreichbar"
    if status != 200:
        return False, f"Lokales Modell meldet HTTP {status}"
    wanted = {s.model, s.text_model}
    missing = [m for m in wanted if m and m not in names and f"{m}:latest" not in names]
    if missing:
        return False, f"Modell nicht installiert: {', '.join(missing)} (vorhanden: {', '.join(names[:8]) or 'keine'})"
    return True, f"Modell bereit: {s.model}"


# --- Ablauf -----------------------------------------------------------------------------------------


def _keep_alive(zq: Zentriq, job_id: int, lease: str, every: float, stop: threading.Event) -> None:
    while not stop.wait(every):
        try:
            status, _ = zq.call(f"/auftrag/{job_id}/verlaengern", {"lease": lease})
            if status == 409:
                log.warning("Auftrag %s: Lease verloren (anderer Lauf übernimmt)", job_id)
                return
        except Exception:  # noqa: BLE001 - Netzfehler: naechster Versuch
            pass


def process(zq: Zentriq, s: Settings, job: dict) -> None:
    job_id, kind, lease = job["id"], job["art"], job["lease"]
    if kind not in s.kinds:  # sollte der Server nie liefern
        zq.call(f"/auftrag/{job_id}/fehler", {"lease": lease, "meldung": "Auftragsart auf diesem Rechner nicht erlaubt", "wiederholen": False})
        return
    started = time.monotonic()
    stop = threading.Event()
    keeper = threading.Thread(target=_keep_alive, args=(zq, job_id, lease, max(20, int(job.get("frist_sekunden") or 300) / 3), stop),
                              daemon=True)
    keeper.start()
    try:
        text = run_model(s, kind, job.get("eingabe") or {})
    except ModelError as exc:
        stop.set()
        log.warning("Auftrag %s (%s): Modellfehler: %s", job_id, kind, exc)
        zq.call(f"/auftrag/{job_id}/fehler", {"lease": lease, "meldung": str(exc), "wiederholen": exc.retryable})
        return
    except Exception as exc:  # noqa: BLE001
        stop.set()
        log.error("Auftrag %s (%s): unerwarteter Fehler %s", job_id, kind, type(exc).__name__)
        zq.call(f"/auftrag/{job_id}/fehler", {"lease": lease, "meldung": f"Fehler auf dem Rechner ({type(exc).__name__})", "wiederholen": True})
        return
    stop.set()
    for attempt in range(5):  # Ergebnis zuverlaessig zurueckmelden (Zentriq uebernimmt es nur einmal)
        try:
            status, data = zq.call(f"/auftrag/{job_id}/ergebnis", {"lease": lease, "text": text}, timeout=60)
            result = (data or {}).get("ergebnis")
            log.info("Auftrag %s (%s) fertig in %.0f s – Zentriq: %s", job_id, kind, time.monotonic() - started, result or status)
            return
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            time.sleep(5 * (attempt + 1))
    log.error("Auftrag %s: Ergebnis konnte nicht gemeldet werden – Zentriq vergibt ihn nach Ablauf der Frist neu", job_id)


def run(s: Settings, once: bool = False) -> int:
    zq = Zentriq(s)
    last_hello = 0.0
    model_ok_until = 0.0
    log.info("Zentriq-Rechner %s gestartet – %s – Arten: %s", VERSION, s.model_label, ", ".join(s.kinds))
    while True:
        try:
            now = time.monotonic()
            if now - last_hello > 60:
                status, data = zq.call("/hallo")
                if status == 401:
                    log.error("Zugang abgelehnt (widerrufen oder falsch). Bitte in Zentriq unter 'Mein Rechner' prüfen.")
                    return 2
                if status == 200 and data:
                    s.poll = int(data.get("abfrage_sekunden") or s.poll)
                last_hello = now
            if now > model_ok_until:
                ok, info = model_ready(s)
                if not ok:  # ohne Modell keine Auftraege abholen (sonst verfallen Versuche)
                    log.warning("%s – warte", info)
                    time.sleep(30)
                    continue
                model_ok_until = now + 300
            status, job = zq.call("/auftrag", {"arten": s.kinds})
            if status == 200 and job:
                process(zq, s, job)
                if once:
                    return 0
                continue
            if status == 401:
                log.error("Zugang abgelehnt (widerrufen oder falsch).")
                return 2
            if once:
                log.info("Kein offener Auftrag.")
                return 0
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            log.warning("Zentriq nicht erreichbar (%s) – neuer Versuch", type(exc).__name__)
            time.sleep(15)
            continue
        time.sleep(s.poll)


def check(s: Settings) -> int:
    problems = s.problems()
    for p in problems:
        print("FEHLER:", p)
    if problems:
        return 1
    try:
        status, data = Zentriq(s).call("/hallo")
    except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
        print("FEHLER: Zentriq nicht erreichbar:", type(exc).__name__)
        return 1
    if status != 200:
        print("FEHLER: Zentriq antwortet mit HTTP", status, "(Zugang widerrufen oder falsch?)" if status == 401 else "")
        return 1
    print("OK: Zentriq erreichbar – Rechner", (data or {}).get("rechner"), "– Arten:", ", ".join((data or {}).get("arten", [])))
    ok, info = model_ready(s)
    print(("OK: " if ok else "FEHLER: ") + info)
    return 0 if ok else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Zentriq-Rechner (lokale KI)")
    parser.add_argument("--config", default=str(Path(__file__).with_name("zentriq-rechner.ini")))
    parser.add_argument("--pruefen", action="store_true", help="Verbindung pruefen und beenden")
    parser.add_argument("--einmal", action="store_true", help="hoechstens einen Auftrag bearbeiten")
    args = parser.parse_args()
    s = Settings(Path(args.config))
    handlers = [logging.StreamHandler(sys.stdout)]
    if s.logfile:
        handlers.append(logging.FileHandler(s.logfile, encoding="utf-8"))
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", handlers=handlers)
    if args.pruefen:
        return check(s)
    problems = s.problems()
    if problems:
        for p in problems:
            log.error("Konfiguration: %s", p)
        return 1
    try:
        return run(s, once=args.einmal)
    except KeyboardInterrupt:
        log.info("Beendet.")
        return 0


if __name__ == "__main__":
    sys.exit(main())
