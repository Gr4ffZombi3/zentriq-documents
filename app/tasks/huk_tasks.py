"""Kfz-Berechnung ueber den HUK-Webrechner im Celery-Worker.

- Hoechstens ein Chromium gleichzeitig (Redis-Sperre, der Server hat wenig Arbeitsspeicher);
  ist der Platz belegt oder zu wenig Speicher frei, wartet der Auftrag ("queued") und versucht es
  kontrolliert erneut (begrenzte Anzahl).
- Laufzeit: Runner-Frist HUK_RUN_TIMEOUT_SECONDS, harte Task-Grenze 300 s.
- Wiederholung nur bei voruebergehenden Fehlern (Netz, Zeitueberschreitung), hoechstens
  HUK_MAX_ATTEMPTS Laeufe; Captcha/Sperre/Antragsbereich werden nie wiederholt.
- Jeder Lauf hat ein eigenes, danach geloeschtes Browserprofil (keine geteilten Sitzungen).
- Protokoll: nur Auftragskennung, Mandant, Fehlercode - keine Eingaben oder Ergebnisse.
resultrepr_maxsize=1: Rueckgabewerte landen nie im Worker-Log."""

import logging
import shutil
import time
from contextlib import contextmanager
from pathlib import Path

from celery import shared_task
from celery.exceptions import SoftTimeLimitExceeded
from flask import current_app

from app.tenancy import use_tenant_id

log = logging.getLogger(__name__)

WAIT_COUNTDOWN = 20
MAX_WAITS = 30  # ca. 10 Minuten auf einen freien Browserplatz
RETRY_COUNTDOWN = 30
LOCK_KEY = "zentriq:huk:browser"


class _Busy(Exception):
    pass


def _free_mb() -> int | None:
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) // 1024
    except OSError:
        return None
    return None


@contextmanager
def browser_slot():
    """Exklusiver Browserplatz. In Tests (eager) ohne Redis."""
    if current_app.config.get("TESTING"):
        yield
        return
    import redis

    client = redis.Redis.from_url(current_app.config["CELERY_BROKER_URL"])
    lock = client.lock(LOCK_KEY, timeout=330, blocking_timeout=0)
    if not lock.acquire(blocking=False):
        raise _Busy()
    try:
        yield
    finally:
        try:
            lock.release()
        except Exception:  # Sperre abgelaufen - nichts zu tun
            pass


def _cancel_checker(job_id: int):
    from app.extensions import db
    from app.models.huk_quote import HukQuoteJob

    state = {"at": 0.0, "value": False}

    def should_cancel() -> bool:
        if state["value"]:
            return True
        now = time.monotonic()
        if now - state["at"] < 2:
            return False
        state["at"] = now
        value = db.session.query(HukQuoteJob.cancel_requested).filter(HukQuoteJob.id == job_id).scalar()
        db.session.rollback()  # keine lange offene Transaktion
        state["value"] = bool(value)
        return state["value"]

    return should_cancel


@shared_task(bind=True, ignore_result=True, soft_time_limit=270, time_limit=300, resultrepr_maxsize=1, max_retries=None)
def run_huk_quote(self, job_id: int, tenant_id: int) -> None:
    from app.extensions import db
    from app.models.huk_quote import HukQuoteJob, utcnow_naive
    from app.services.huk import jobs, runner

    with use_tenant_id(tenant_id):
        job = HukQuoteJob.query.filter(HukQuoteJob.id == job_id).first()
        if job is None or job.status not in ("queued", "running"):
            return
        if job.cancel_requested:
            jobs._finish(job, "cancelled", "cancelled", "Die Berechnung wurde abgebrochen.")
            return
        cfg = current_app.config
        waits = int(getattr(self.request, "retries", 0) or 0)

        free = _free_mb()
        if not cfg.get("TESTING") and free is not None and free < int(cfg.get("HUK_MIN_FREE_MB") or 350):
            return _wait(self, job, waits, "Wartet auf freie Serverkapazität …", "memory", free_mb=free)

        try:
            with browser_slot():
                _execute(job, cfg, jobs, runner, db, utcnow_naive)
        except _Busy:
            return _wait(self, job, waits, "Wartet, bis eine andere Berechnung fertig ist …", "busy")
        except SoftTimeLimitExceeded:
            db.session.rollback()
            log.warning("huk.job.hard_timeout job_id=%s tenant_id=%s", job_id, tenant_id)
            job = HukQuoteJob.query.filter(HukQuoteJob.id == job_id).first()
            if job is not None and job.is_active:
                jobs._finish(job, "failed", "timeout", "Die Berechnung hat zu lange gedauert und wurde beendet.")
            return

        if job.status == "queued" and job.error_code == "retry":
            # Voruebergehender Fehler: kontrollierte Wiederholung.
            raise self.retry(countdown=RETRY_COUNTDOWN, max_retries=MAX_WAITS + int(cfg.get("HUK_MAX_ATTEMPTS") or 2))


def _wait(task, job, waits, message, reason, free_mb=None):
    from app.extensions import db
    from app.services.huk import jobs

    if waits >= MAX_WAITS:
        log.warning("huk.job.gave_up job_id=%s reason=%s", job.id, reason)
        jobs._finish(job, "failed", reason, "Der Server war längere Zeit ausgelastet. Bitte später erneut starten.")
        return
    log.info("huk.job.wait job_id=%s reason=%s free_mb=%s", job.id, reason, free_mb)
    from app.models.huk_quote import utcnow_naive

    job.message = message
    job.updated_at = utcnow_naive()  # Lebenszeichen fuer expire_stale()
    db.session.commit()
    raise task.retry(countdown=WAIT_COUNTDOWN, max_retries=MAX_WAITS + 5)


def _execute(job, cfg, jobs, runner, db, utcnow_naive):
    jobs.cleanup_files()
    job.status = "running"
    job.attempts = (job.attempts or 0) + 1
    job.started_at = job.started_at or utcnow_naive()
    job.message = None
    job.error_code = None
    db.session.commit()
    values = dict(job.input_data or {})
    missing, errors = jobs.check_complete(job)
    if missing or errors:
        jobs._finish(job, "needs_input", "incomplete", "Es fehlen Angaben. Bitte das Formular vervollständigen.")
        return

    storage = Path(cfg["HUK_STORAGE_DIR"]) / str(job.tenant_id)
    storage.mkdir(parents=True, exist_ok=True)
    evidence_rel = f"{job.tenant_id}/{job.id}.png"
    profile = Path(cfg["HUK_BROWSER_TMP_DIR"]) / f"t{job.tenant_id}-j{job.id}-a{job.attempts}"
    profile.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    try:
        result = runner.run(
            values,
            timeout_s=int(cfg.get("HUK_RUN_TIMEOUT_SECONDS") or 150),
            should_cancel=_cancel_checker(job.id),
            evidence_path=str(storage / f"{job.id}.png"),
            profile_dir=str(profile),
        )
    except runner.HukRunError as exc:
        db.session.rollback()
        log.warning("huk.job.error job_id=%s tenant_id=%s code=%s attempt=%s seconds=%.0f", job.id, job.tenant_id,
                    exc.code, job.attempts, time.monotonic() - started)
        if exc.code == "cancelled":
            jobs._finish(job, "cancelled", "cancelled", "Die Berechnung wurde abgebrochen.")
        elif exc.retryable and job.attempts < int(cfg.get("HUK_MAX_ATTEMPTS") or 2):
            job.status = "queued"
            job.error_code = "retry"
            job.message = "Der Rechner hat nicht reagiert – neuer Versuch …"
            db.session.commit()
        elif exc.code == "needs_input":
            jobs._finish(job, "needs_input", exc.code, exc.message)
        else:
            jobs._finish(job, "failed", exc.code, exc.message)
        return
    finally:
        shutil.rmtree(profile, ignore_errors=True)

    db.session.refresh(job)
    if job.cancel_requested:
        jobs._finish(job, "cancelled", "cancelled", "Die Berechnung wurde abgebrochen. Es wurde kein Ergebnis gespeichert.")
        return
    job.result = {
        **result,
        "source": "huk.de",
        "source_url": runner.START_URL,
        "calculated_at": utcnow_naive().isoformat(timespec="seconds") + "Z",
        "kind": "unverbindliche Beitragsberechnung",
    }
    job.evidence_path = evidence_rel if (storage / f"{job.id}.png").is_file() else None
    log.info("huk.job.done job_id=%s tenant_id=%s seconds=%.0f tariffs=%s", job.id, job.tenant_id,
             time.monotonic() - started, len(result.get("tariffs") or []))
    jobs._finish(job, "done", None, None)
